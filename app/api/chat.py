import logging

from fastapi import APIRouter, HTTPException, Response, status

from app.kafka.producer import publish_chat_request
from app.redis_client import get_redis
from app.schemas import ChatAskRequest, ChatAskResponse
from app.services import idempotency, quota

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/ask", response_model=ChatAskResponse, status_code=status.HTTP_202_ACCEPTED)
async def ask(payload: ChatAskRequest, response: Response) -> ChatAskResponse:
    """
    Nhận câu hỏi của user, check quota + idempotency rồi mới đẩy vào Kafka.

    Thứ tự CỐ Ý: idempotency trước, quota sau. Nếu check quota trước thì mỗi
    lần client retry (cùng request_id) đều trừ thêm 1 lượt, user mất quota vì
    mạng lỗi chứ không phải vì hỏi nhiều.
    """
    redis = get_redis()
    request_id = payload.request_id

    is_new = await idempotency.claim(redis, request_id)
    if not is_new:
        cached = await idempotency.get_cached_response(redis, request_id)
        logger.info(
            "Duplicate request_id=%s user_id=%s (đã có câu trả lời: %s)",
            request_id,
            payload.user_id,
            cached is not None,
        )
        response.status_code = status.HTTP_200_OK
        return ChatAskResponse(
            request_id=request_id,
            status="done" if cached else "duplicate",
            answer=cached,
            quota_remaining=await quota.remaining(redis, payload.user_id, payload.tier),
        )

    try:
        remaining = await quota.consume(redis, payload.user_id, payload.tier)
    except quota.QuotaExceeded as exc:
        # Nhả claim: request này chưa hề được xử lý, để user gửi lại được vào
        # ngày mai với đúng request_id đó.
        await idempotency.release(redis, request_id)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)
        ) from exc

    try:
        await publish_chat_request(payload.to_message())
    except Exception:
        # Enqueue lỗi = user chưa hỏi được gì, không được tính quota.
        await quota.refund(redis, payload.user_id)
        await idempotency.release(redis, request_id)
        logger.exception("Enqueue thất bại cho request_id=%s", request_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Hệ thống đang bận, vui lòng thử lại sau ít phút.",
        )

    return ChatAskResponse(
        request_id=request_id, status="accepted", quota_remaining=remaining
    )
