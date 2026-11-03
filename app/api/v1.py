"""
API cho app mobile (hợp đồng: PatronyApp docs/ai-chat-api.md).

`POST /v1/chat` — khác `/chat/ask` ở chỗ câu trả lời về NGAY TRONG response
(SSE `delta` ... `done`, hoặc JSON), thay vì 202 rồi nghe ở `/chat/stream`.
Bên trong vẫn đi đúng pipeline cũ (quota -> Kafka -> consumer -> OpenAI ->
validate): handler đăng ký nghe hub SSE của user TRƯỚC khi publish, rồi chờ
response mang đúng request_id. Response consumer chạy trên mọi instance nên câu
trả lời luôn về được instance đang giữ request này; Redis (câu trả lời đã cache)
là đường dự phòng nếu response consumer chậm hoặc chết.

`POST /v1/report/monthly` — tóm tắt báo cáo sức khoẻ tháng, gọi OpenAI trực
tiếp (một lần, không ngữ cảnh), cache theo nội dung số liệu.

`POST /v1/facility/suggest` — gợi ý cơ sở khám theo triệu chứng (KAN-115).

`GET /v1/conversations`, `GET /v1/conversations/{id}/messages` — lịch sử chat
cho app: đọc lại đúng những lượt consumer đã ghi vào DB.
`DELETE /v1/conversations[/{id}]` — user xoá lịch sử: ẩn ngay, xoá hẳn khỏi DB
sau HISTORY_PURGE_AFTER_DAYS ngày (app/services/history.py).

Lỗi luôn có dạng `{"error": <mã>, "message": <câu hiển thị được>}`:
  401 unauthorized        — token hỏng/hết hạn: app đăng nhập lại.
  403 consent_required    — chưa đồng ý dùng Trợ lý AI: app hiện màn đồng ý.
  403 not_enabled         — tài khoản chưa nằm trong nhóm được mở (rollout).
  404 not_found           — không có hội thoại này (hoặc của người khác).
  422 invalid_request     — body sai hợp đồng.
  429 quota_exceeded      — hết lượt hỏi trong ngày (kèm retry_after, limit).
  429 rate_limited        — gọi dồn dập / quá số lần tạo báo cáo trong ngày.
  503 unavailable         — lỗi phía service: app cho thử lại.
"""

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from uuid import UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from app import metrics
from app.auth import AuthError, AuthUnavailable, Principal, authenticate
from app.config import settings
from app.db import repository
from app.db.session import db_session
from app.kafka.producer import publish_chat_request
from app.redis_client import get_redis
from app.schemas import (
    ChatAnswer,
    ChatRequestMessage,
    ChatResponseMessage,
    V1ChatRequest,
    V1FacilitySuggestRequest,
    V1MonthlyReportRequest,
)
from app.services import consent, cost, idempotency, openai_client, output_validator, quota, rollout, smalltalk
from app.services.facility import facilities
from app.services.facility import suggest as facility_suggest_service
from app.sse import hub

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["v1"])

# Độ dài tối đa câu hỏi — bằng giới hạn của /chat/ask.
QUESTION_MAX_CHARS = 2000
# Cỡ mỗi `delta`. Câu trả lời sinh xong một lượt (structured output) rồi mới
# chia nhỏ, nên chỉ cần đủ nhỏ để app hiện chữ dần cho tự nhiên.
DELTA_CHARS = 40
DELTA_INTERVAL_SECONDS = 0.03
# Không có response trong hub ngần này giây thì nhìn sang Redis một lần.
POLL_SECONDS = 1.0
# Cỡ trang lịch sử: (mặc định, tối đa).
CONVERSATIONS_PAGE = (20, 50)
MESSAGES_PAGE = (50, 100)
TITLE_CHARS = 80
PREVIEW_CHARS = 160
# Namespace cho request_id dẫn xuất từ Idempotency-Key (xem `_request_id`).
_IDEMPOTENCY_NS = UUID("6f1d0c4e-2b8a-4f57-9a43-5f0f3b7c2e10")

CRISIS_ANSWER = ChatAnswer(
    answer=(
        "Mình rất tiếc khi bạn đang phải trải qua điều này, và bạn không phải "
        "một mình. Nếu bạn đang có ý định làm hại bản thân hoặc đang gặp nguy "
        "hiểm, hãy gọi 115 ngay hoặc nhờ một người thân ở bên cạnh bạn. Bạn cũng "
        "có thể liên hệ các đường dây hỗ trợ tâm lý trên màn hình hỗ trợ của ứng "
        "dụng — ở đó có người sẵn sàng lắng nghe bạn."
    ),
    out_of_scope=False,
    refusal_reason="",
    should_see_doctor=True,
    follow_up_questions=[],
)


class V1Error(Exception):
    def __init__(self, status_code: int, code: str, message: str, **extra):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra
        super().__init__(message)


async def _v1_error_handler(request: Request, exc: V1Error) -> JSONResponse:
    headers = {}
    if "retry_after" in exc.extra:
        headers["Retry-After"] = str(exc.extra["retry_after"])
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message, **exc.extra},
        headers=headers,
    )


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.add_exception_handler(V1Error, _v1_error_handler)


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


async def _principal(request: Request) -> Principal:
    if not settings.auth_required:
        return Principal(
            user_id=request.query_params.get("user_id", "0"),
            tier=request.query_params.get("tier", "free"),
        )
    token = _bearer(request)
    if not token:
        raise V1Error(401, "unauthorized", "Phiên đăng nhập không hợp lệ, vui lòng đăng nhập lại.")
    try:
        return await authenticate(token)
    except AuthUnavailable as exc:
        raise V1Error(503, "unavailable", "Hệ thống đang bận, vui lòng thử lại sau ít phút.") from exc
    except AuthError as exc:
        raise V1Error(401, "unauthorized", "Phiên đăng nhập đã hết hạn, vui lòng đăng nhập lại.") from exc


async def _gate(principal: Principal, request: Request) -> consent.Consent:
    """Rollout + đồng ý. Dùng chung cho chat và báo cáo. Đọc đồng ý bằng
    chính token của user (RLS), không dùng key quyền cao của server."""
    if not rollout.is_enabled(principal.user_id):
        metrics.incr("v1_not_in_rollout")
        raise V1Error(403, "not_enabled", "Trợ lý AI chưa được mở cho tài khoản của bạn.")
    try:
        granted = await consent.check(get_redis(), principal.user_id, _bearer(request))
    except consent.ConsentUnavailable as exc:
        metrics.incr("consent_unavailable")
        raise V1Error(503, "unavailable", "Hệ thống đang bận, vui lòng thử lại sau ít phút.") from exc
    if not granted.ai_chat:
        metrics.incr("v1_consent_required")
        raise V1Error(403, "consent_required", "Bạn cần đồng ý điều khoản Trợ lý AI trước khi sử dụng.")
    return granted


def _conversation_id(raw: str | None) -> UUID:
    """conversation_id do service cấp (UUID). Không có, hoặc không phải id do
    service cấp, thì mở hội thoại mới."""
    if raw:
        try:
            return UUID(raw)
        except ValueError:
            pass
    return uuid4()


def _request_id(user_id: str, request: Request) -> UUID:
    """App gửi `Idempotency-Key` (giữ nguyên khi thử lại vì rớt mạng) thì lần
    thử lại không bị tính thêm lượt. Gắn user_id vào để key của hai người trùng
    nhau không bao giờ thành cùng một request."""
    key = request.headers.get("idempotency-key", "").strip()
    if key:
        return uuid5(_IDEMPOTENCY_NS, f"{user_id}:{key[:128]}")
    return uuid4()


def render_health_context(data: dict | None) -> str | None:
    """Phẳng hoá hồ sơ thành các dòng `khoá: giá trị`. Không chở nguyên JSON
    vào prompt: ngoặc nhọn, dấu nháy tốn token mà không thêm nghĩa."""
    if not data:
        return None
    lines: list[str] = []

    def walk(prefix: str, value) -> None:
        if len(lines) >= 60:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                walk(f"{prefix}.{k}" if prefix else str(k), v)
        elif isinstance(value, list):
            items = [str(v) for v in value if not isinstance(v, (dict, list))]
            nested = [v for v in value if isinstance(v, (dict, list))]
            if items:
                lines.append(f"{prefix}: {', '.join(items)[:300]}")
            for v in nested:
                walk(prefix, v)
        elif value is not None and str(value).strip():
            lines.append(f"{prefix}: {str(value)[:300]}")

    walk("", data)
    return "\n".join(lines) or None


def _chunks(text: str) -> list[str]:
    """Chia câu trả lời theo ranh giới từ; nối lại các phần ra đúng nguyên văn."""
    parts, current = [], ""
    for token in re.findall(r"\S+\s*", text):
        if current and len(current) + len(token) > DELTA_CHARS:
            parts.append(current)
            current = ""
        current += token
    if current:
        parts.append(current)
    return parts


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _wants_sse(request: Request) -> bool:
    accept = request.headers.get("accept", "").lower()
    if "text/event-stream" in accept:
        return True
    # Đặc tả khuyến nghị SSE; chỉ trả JSON khi app hỏi đích danh JSON.
    return "application/json" not in accept


async def _wait_for(queue: asyncio.Queue, request_id: UUID, user_id: str) -> ChatResponseMessage | None:
    """Chờ câu trả lời của đúng request_id. None = quá hạn."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.v1_answer_timeout_seconds
    while (remaining := deadline - loop.time()) > 0:
        try:
            response = await asyncio.wait_for(queue.get(), timeout=min(POLL_SECONDS, remaining))
        except asyncio.TimeoutError:
            cached = await idempotency.get_cached_response(get_redis(), request_id)
            if cached is not None and str(cached.get("user_id")) == user_id:
                return ChatResponseMessage.model_validate(cached)
            continue
        # Hub giao MỌI response của user (vd câu hỏi từ máy khác), lọc đúng câu
        # này; "processing" chỉ là tín hiệu giữ nhịp của /chat/stream.
        if response.request_id == request_id and response.status != "processing":
            return response
    return None


def _done_payload(answer: ChatAnswer, conversation_id: UUID, quota_remaining: int | None) -> dict:
    return {
        "type": "done",
        "conversation_id": str(conversation_id),
        "should_see_doctor": answer.should_see_doctor,
        "follow_up_questions": answer.follow_up_questions,
        "quota_remaining": quota_remaining,
    }


async def _stream_answer(answer: ChatAnswer, conversation_id: UUID, quota_remaining: int | None):
    for index, part in enumerate(_chunks(answer.answer)):
        if index:
            await asyncio.sleep(DELTA_INTERVAL_SECONDS)
        yield _sse({"type": "delta", "text": part})
    yield _sse(_done_payload(answer, conversation_id, quota_remaining))


_UNAVAILABLE_EVENT = {
    "type": "error",
    "code": "unavailable",
    "message": "Xin lỗi, hệ thống đang gặp sự cố nên chưa trả lời được. Lượt hỏi này không bị tính, bạn thử lại sau ít phút nhé.",
}

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    # Tắt buffer của nginx, nếu không nginx gom cả câu trả lời rồi mới đẩy.
    "X-Accel-Buffering": "no",
}


def _respond_now(request: Request, answer: ChatAnswer, conversation_id: UUID, quota_remaining: int | None):
    if _wants_sse(request):
        return StreamingResponse(
            _stream_answer(answer, conversation_id, quota_remaining),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )
    return JSONResponse(_json_body(answer, conversation_id, quota_remaining))


def _json_body(answer: ChatAnswer, conversation_id: UUID, quota_remaining: int | None) -> dict:
    return {
        "reply": answer.answer,
        "conversation_id": str(conversation_id),
        "should_see_doctor": answer.should_see_doctor,
        "follow_up_questions": answer.follow_up_questions,
        "quota_remaining": quota_remaining,
    }


@router.post("/chat")
async def chat(payload: V1ChatRequest, request: Request):
    principal = await _principal(request)
    metrics.incr("v1_chat_requests")
    granted = await _gate(principal, request)

    last = payload.messages[-1]
    question = last.content.strip()
    if last.role != "user" or not question:
        raise V1Error(422, "invalid_request", "Tin nhắn cuối cùng phải là câu hỏi của người dùng.")
    if len(question) > QUESTION_MAX_CHARS:
        raise V1Error(
            422, "message_too_long",
            f"Câu hỏi dài quá {QUESTION_MAX_CHARS} ký tự, bạn rút gọn giúp mình nhé.",
            max_chars=QUESTION_MAX_CHARS,
        )
    conversation_id = _conversation_id(payload.conversation_id)

    if payload.client_safety != "none":
        # App đã hiện 115 / màn hỗ trợ tâm lý. Không gọi model cho lượt này:
        # câu trả lời đã biết trước, không được để model "sáng tạo", và không
        # tính lượt — người đang khủng hoảng không được gặp "hết lượt hôm nay".
        metrics.incr(f"v1_client_safety_{payload.client_safety}")
        answer = (
            output_validator.EMERGENCY_FALLBACK_ANSWER
            if payload.client_safety == "emergency"
            else CRISIS_ANSWER
        )
        return _respond_now(request, answer, conversation_id, None)

    redis = get_redis()
    if smalltalk.is_greeting(question):
        # Câu chào: trả lời cố định, không gọi model, không tính lượt.
        metrics.incr("v1_greeting")
        remaining = await quota.remaining(redis, principal.user_id, principal.tier)
        return _respond_now(request, smalltalk.GREETING_ANSWER, conversation_id, remaining)

    request_id = _request_id(principal.user_id, request)
    # Nghe TRƯỚC khi publish: câu bị chặn ở lớp lọc input trả về trong vài ms,
    # đăng ký sau là lỡ mất.
    queue = hub.subscribe(principal.user_id)
    try:
        quota_remaining = await _enqueue(
            redis, principal, request_id, conversation_id, question,
            render_health_context(payload.health_context) if granted.share_profile else None,
        )
    except BaseException:
        hub.unsubscribe(principal.user_id, queue)
        raise

    if _wants_sse(request):
        return StreamingResponse(
            _stream_pipeline(request, queue, principal.user_id, request_id, conversation_id, quota_remaining),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )
    try:
        response = await _wait_for(queue, request_id, principal.user_id)
    finally:
        hub.unsubscribe(principal.user_id, queue)
    if response is None or response.status == "error":
        metrics.incr("v1_chat_unavailable")
        raise V1Error(503, "unavailable", _UNAVAILABLE_EVENT["message"])
    return JSONResponse(_json_body(response.answer, conversation_id, quota_remaining))


async def _enqueue(redis, principal, request_id, conversation_id, question, health_context) -> int | None:
    """Idempotency -> quota -> Kafka, cùng thứ tự với /chat/ask. Trả về số lượt
    còn lại. Request trùng (thử lại cùng Idempotency-Key) không trừ thêm lượt
    và không publish lại — chỉ chờ câu trả lời của lần đầu."""
    if not await idempotency.claim(redis, request_id):
        metrics.incr("v1_duplicate")
        return await quota.remaining(redis, principal.user_id, principal.tier)

    try:
        charged = await quota.charge(redis, principal.user_id, principal.tier)
    except quota.QuotaExceeded as exc:
        await idempotency.release(redis, request_id)
        metrics.incr("v1_quota_exceeded")
        raise V1Error(
            429, "quota_exceeded",
            f"Hôm nay bạn đã dùng hết {exc.limit} câu hỏi. Lượt hỏi sẽ được làm mới vào 0h.",
            limit=exc.limit,
            retry_after=quota.seconds_until_midnight(),
        ) from exc

    try:
        await publish_chat_request(
            ChatRequestMessage(
                request_id=request_id,
                user_id=principal.user_id,
                session_id=conversation_id,
                tier=principal.tier,
                content=question,
                question_no=charged.question_no,
                health_context=health_context,
            )
        )
    except Exception as exc:
        await quota.refund(redis, principal.user_id)
        await idempotency.release(redis, request_id)
        metrics.incr("v1_enqueue_failed")
        logger.exception("Enqueue thất bại request_id=%s", request_id)
        raise V1Error(503, "unavailable", "Hệ thống đang bận, vui lòng thử lại sau ít phút.") from exc
    metrics.incr("v1_chat_accepted")
    return charged.remaining


async def _stream_pipeline(request, queue, user_id, request_id, conversation_id, quota_remaining):
    try:
        # Byte đầu tiên đi ngay để proxy/app biết kết nối đã thông.
        yield ": ok\n\n"
        waiter = asyncio.create_task(_wait_for(queue, request_id, user_id))
        try:
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=settings.sse_heartbeat_seconds)
                if done:
                    break
                if await request.is_disconnected():
                    # Câu trả lời vẫn được sinh và ghi lịch sử ở consumer; app
                    # mở lại hội thoại sẽ thấy trong ngữ cảnh.
                    return
                yield ": ping\n\n"
        finally:
            if not waiter.done():
                waiter.cancel()
        response = waiter.result()
        if response is None or response.status == "error":
            metrics.incr("v1_chat_unavailable")
            yield _sse(_UNAVAILABLE_EVENT)
            return
        async for frame in _stream_answer(response.answer, conversation_id, quota_remaining):
            yield frame
    finally:
        hub.unsubscribe(user_id, queue)


def _today() -> str:
    return datetime.now(ZoneInfo(settings.quota_timezone)).strftime("%Y-%m-%d")


@router.post("/report/monthly")
async def monthly_report(payload: V1MonthlyReportRequest, request: Request):
    principal = await _principal(request)
    metrics.incr("v1_report_requests")
    await _gate(principal, request)

    data = payload.model_dump(exclude={"locale"})
    report_data = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if not data:
        raise V1Error(422, "invalid_request", "Thiếu số liệu báo cáo.")
    if len(report_data) > settings.report_input_max_chars:
        raise V1Error(413, "payload_too_large", "Số liệu báo cáo quá lớn.")

    redis = get_redis()
    digest = hashlib.sha256(report_data.encode()).hexdigest()
    cache_key = f"report:{principal.user_id}:{settings.report_prompt_version}:{digest}"
    cached = await redis.get(cache_key)
    if cached:
        metrics.incr("v1_report_cached")
        return {"summary": cached}

    # Đếm theo ngày: cùng số liệu thì đã có cache, nên trần này chỉ chặn việc
    # gửi số liệu khác nhau liên tục để đốt chi phí.
    counter = f"report_count:{principal.user_id}:{_today()}"
    used = await redis.incr(counter)
    if used == 1:
        await redis.expire(counter, quota.seconds_until_midnight())
    if used > settings.report_max_per_day:
        await redis.decr(counter)
        raise V1Error(
            429, "rate_limited", "Bạn đã tạo báo cáo nhiều lần hôm nay, vui lòng thử lại vào ngày mai.",
            retry_after=quota.seconds_until_midnight(),
        )

    try:
        result, usage = await openai_client.generate_report(report_data)
    except Exception as exc:
        await redis.decr(counter)
        metrics.incr("v1_report_failed")
        logger.exception("Tạo tóm tắt báo cáo thất bại user_id=%s", principal.user_id)
        raise V1Error(503, "unavailable", "Chưa tạo được bản tóm tắt, bạn thử lại sau nhé.") from exc

    cost.record(usage, settings.openai_model)

    summary = result.summary.strip()
    problems = output_validator.report_problems(summary, settings.report_prompt_version)
    if problems:
        await redis.decr(counter)
        metrics.incr("v1_report_blocked")
        logger.warning("Tóm tắt báo cáo không qua kiểm tra user_id=%s reasons=%s", principal.user_id, problems)
        raise V1Error(503, "unavailable", "Chưa tạo được bản tóm tắt, bạn thử lại sau nhé.")

    await redis.set(cache_key, summary, ex=settings.report_cache_seconds)
    return {"summary": summary}


@router.post("/facility/suggest")
async def facility_suggest(payload: V1FacilitySuggestRequest, request: Request):
    """Gợi ý cơ sở khám theo triệu chứng (KAN-115, docs/facility-suggest.md).
    Không trừ lượt chat. Trần riêng FACILITY_MAX_PER_DAY chỉ tính những lần
    đọc câu gõ bằng model; lượt chỉ có thẻ hay dùng lại kết quả đã cache thì
    không tính."""
    principal = await _principal(request)
    metrics.incr("v1_facility_requests")
    await _gate(principal, request)

    redis = get_redis()
    counter = f"facility_count:{principal.user_id}:{_today()}"

    async def charge() -> None:
        used = await redis.incr(counter)
        if used == 1:
            await redis.expire(counter, quota.seconds_until_midnight())
        if used > settings.facility_max_per_day:
            await redis.decr(counter)
            raise facility_suggest_service.LimitReached

    async def refund() -> None:
        await redis.decr(counter)

    try:
        return await facility_suggest_service.suggest(
            redis,
            age=payload.profile.age,
            symptom_tags=payload.symptom_tags,
            text=payload.text,
            lat=payload.location.lat,
            lng=payload.location.lng,
            radius_km=payload.radius_km,
            limit=payload.limit,
            clarify_choice=payload.clarify_choice,
            charge=charge,
            refund=refund,
        )
    except facility_suggest_service.InvalidRequest as exc:
        raise V1Error(422, "invalid_request", str(exc)) from exc
    except facility_suggest_service.LimitReached as exc:
        raise V1Error(
            429, "rate_limited", "Bạn đã tìm nơi khám bằng mô tả nhiều lần hôm nay, vui lòng chọn thẻ triệu chứng "
            "hoặc thử lại vào ngày mai.",
            retry_after=quota.seconds_until_midnight(),
        ) from exc
    except facilities.FacilitiesUnavailable as exc:
        metrics.incr("v1_facility_unavailable")
        raise V1Error(503, "unavailable", "Chưa tải được danh sách cơ sở khám, bạn thử lại sau nhé.") from exc


# --- Lịch sử chat -----------------------------------------------------------
# Chỉ cần đăng nhập, không qua `_gate`: đây là dữ liệu của chính user, rút
# đồng ý hay ra khỏi rollout không có nghĩa là mất quyền xem lại hay xoá những
# gì mình đã hỏi. Lọc theo user_id của token ở tầng truy vấn.

_HISTORY_UNAVAILABLE = "Chưa tải được lịch sử trò chuyện, bạn thử lại sau nhé."


def _page(request: Request, sizes: tuple[int, int]) -> tuple[int, int | None]:
    """`limit` và `cursor` từ query. Đọc tay thay vì để FastAPI validate: lỗi
    của FastAPI không theo dạng `{"error", "message"}` của /v1. `limit` quá
    trần thì kẹp lại, không báo lỗi."""
    default, maximum = sizes
    raw_limit = request.query_params.get("limit", "")
    raw_cursor = request.query_params.get("cursor", "")
    for raw in (raw_limit, raw_cursor):
        if raw and not re.fullmatch(r"[0-9]{1,18}", raw):
            raise V1Error(422, "invalid_request", "Tham số phân trang không hợp lệ.")
    limit = min(max(int(raw_limit), 1), maximum) if raw_limit else default
    return limit, int(raw_cursor) if raw_cursor else None


def _iso(value: datetime) -> str:
    # SQLite (test) trả datetime không kèm múi giờ; giá trị lưu là UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _message_body(message) -> dict:
    body = {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "created_at": _iso(message.created_at),
    }
    if message.role == "assistant":
        body["should_see_doctor"] = bool(message.meta.get("should_see_doctor", False))
        # Lượt ghi trước khi lưu gợi ý vào meta thì không có.
        body["follow_up_questions"] = message.meta.get("follow_up_questions", [])
    return body


@router.get("/conversations")
async def conversations(request: Request):
    principal = await _principal(request)
    metrics.incr("v1_history_conversations")
    limit, before = _page(request, CONVERSATIONS_PAGE)
    try:
        async with db_session() as db:
            rows = await repository.list_conversations(db, principal.user_id, limit + 1, before)
    except Exception as exc:
        metrics.incr("v1_history_unavailable")
        logger.exception("Đọc danh sách hội thoại thất bại user_id=%s", principal.user_id)
        raise V1Error(503, "unavailable", _HISTORY_UNAVAILABLE) from exc

    page = rows[:limit]
    return {
        "conversations": [
            {
                "conversation_id": str(row.session_id),
                "title": _clip(row.first.content, TITLE_CHARS),
                "preview": _clip(row.last.content, PREVIEW_CHARS),
                "message_count": row.message_count,
                "created_at": _iso(row.first.created_at),
                "updated_at": _iso(row.last.created_at),
            }
            for row in page
        ],
        "next_cursor": str(page[-1].last.id) if len(rows) > limit else None,
    }


@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(conversation_id: str, request: Request):
    principal = await _principal(request)
    metrics.incr("v1_history_messages")
    limit, before = _page(request, MESSAGES_PAGE)
    not_found = V1Error(404, "not_found", "Không tìm thấy cuộc trò chuyện này.")
    try:
        session_id = UUID(conversation_id)
    except ValueError:
        raise not_found from None
    try:
        async with db_session() as db:
            rows = await repository.list_messages(db, session_id, principal.user_id, limit + 1, before)
    except Exception as exc:
        metrics.incr("v1_history_unavailable")
        logger.exception("Đọc lịch sử hội thoại thất bại user_id=%s", principal.user_id)
        raise V1Error(503, "unavailable", _HISTORY_UNAVAILABLE) from exc
    if rows is None:
        raise not_found

    # Lấy dư 1 để biết còn trang cũ hơn; dòng dư là dòng CŨ nhất, nằm đầu list.
    page = rows[-limit:]
    return {
        "conversation_id": str(session_id),
        "messages": [_message_body(m) for m in page],
        "next_cursor": str(page[0].id) if len(rows) > limit else None,
    }


_DELETE_UNAVAILABLE = "Chưa xoá được lịch sử trò chuyện, bạn thử lại sau nhé."


@router.delete("/conversations")
async def delete_conversations(request: Request):
    """Xoá mọi hội thoại của user. App gọi TRƯỚC khi xoá tài khoản Supabase:
    xoá tài khoản xong thì token không còn dùng được nữa."""
    principal = await _principal(request)
    metrics.incr("v1_history_delete_all")
    try:
        async with db_session() as db:
            deleted = await repository.soft_delete_all(db, principal.user_id)
    except Exception as exc:
        metrics.incr("v1_history_delete_failed")
        logger.exception("Xoá toàn bộ lịch sử thất bại user_id=%s", principal.user_id)
        raise V1Error(503, "unavailable", _DELETE_UNAVAILABLE) from exc
    logger.info("Xoá toàn bộ lịch sử user_id=%s conversations=%d", principal.user_id, deleted)
    return Response(status_code=204)


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str, request: Request):
    principal = await _principal(request)
    metrics.incr("v1_history_delete")
    not_found = V1Error(404, "not_found", "Không tìm thấy cuộc trò chuyện này.")
    try:
        session_id = UUID(conversation_id)
    except ValueError:
        raise not_found from None
    try:
        async with db_session() as db:
            deleted = await repository.soft_delete_conversation(db, session_id, principal.user_id)
    except Exception as exc:
        metrics.incr("v1_history_delete_failed")
        logger.exception("Xoá hội thoại thất bại user_id=%s", principal.user_id)
        raise V1Error(503, "unavailable", _DELETE_UNAVAILABLE) from exc
    if not deleted:
        raise not_found
    return Response(status_code=204)
