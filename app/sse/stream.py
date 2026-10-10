"""
Endpoint SSE: `GET /chat/stream`.

`user_id` lấy từ token đã ký, không phải từ query param (trước Phase 6 thì có,
và ai cũng nghe được câu trả lời sức khoẻ của người khác nếu đoán được user_id).

Token phải đi qua **query param** `?token=...` chứ không phải header
Authorization: EventSource của trình duyệt không gửi được header tuỳ ý. Đánh
đổi: token nằm trong access log của proxy, nên loại token này cần TTL ngắn.
"""

import asyncio
import json
import logging
import time
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app import metrics
from app.auth import Principal, current_principal

from app.config import settings
from app.redis_client import get_redis
from app.schemas import ChatResponseMessage
from app.sse import hub, replay

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


# Client mất kết nối thì EventSource tự nối lại sau ngần này ms.
RECONNECT_MS = 3000


def format_event(response: ChatResponseMessage) -> str:
    """
    Một khung SSE. `id:` để client tự nhớ mốc cuối cùng nhận được và gửi lại
    khi reconnect (trình duyệt tự gửi header `Last-Event-ID`).

    Event `processing` KHÔNG có `id:`. Nó mang cùng request_id với câu trả lời
    thật, và không nằm trong bộ đệm replay: có `id:` thì client rớt mạng ngay
    sau event này sẽ reconnect với mốc không tìm thấy trong bộ đệm, và nhận lại
    toàn bộ bộ đệm thay vì đúng phần đã lỡ.
    """
    payload = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
    frame = f"event: {response.status}\ndata: {payload}\n\n"
    if response.status == "processing":
        return frame
    return f"id: {response.request_id}\n" + frame


def ping_event() -> str:
    """
    Heartbeat. Dùng event có tên thay vì comment (`: keepalive`): comment giữ
    được kết nối qua proxy/LB, nhưng EventSource không cho JS thấy comment —
    client không tự phát hiện được kết nối đã chết "nửa vời" (mạng di động đổi
    sóng, NAT hết hạn) để chủ động nối lại. Có `ping` thì client đặt đồng hồ:
    quá ~2 nhịp không thấy ping là tự đóng và mở lại.
    """
    return f"event: ping\ndata: {int(time.time())}\n\n"


async def event_stream(request: Request, user_id: str, last_request_id: UUID | None):
    queue = hub.subscribe(user_id)
    try:
        # Gửi ngay 1 khung khi vừa mở: đặt nhịp reconnect cho EventSource, và để
        # proxy/LB đẩy header + byte đầu tiên đi luôn thay vì chờ tới nhịp
        # heartbeat đầu tiên (một số LB coi 15s không có byte nào là treo).
        yield f"retry: {RECONNECT_MS}\n" + ping_event()

        for missed in await replay.missed_since(get_redis(), user_id, last_request_id):
            yield format_event(missed)

        deadline = time.monotonic() + settings.sse_max_connection_seconds
        while True:
            if await request.is_disconnected():
                break
            if time.monotonic() >= deadline:
                # Đóng chủ động; EventSource nối lại sau RECONNECT_MS kèm
                # Last-Event-ID nên không lỡ event nào, và token được kiểm lại.
                break
            try:
                response = await asyncio.wait_for(
                    queue.get(), timeout=settings.sse_heartbeat_seconds
                )
            except asyncio.TimeoutError:
                # Proxy/LB thường đóng kết nối idle sau 30-60s.
                yield ping_event()
                continue
            yield format_event(response)
    finally:
        hub.unsubscribe(user_id, queue)


@router.get("/stream")
async def stream(
    request: Request,
    last_request_id: UUID | None = Query(
        None,
        description="request_id cuối cùng client đã nhận; dùng để phát lại phần đã lỡ",
    ),
    last_event_id: UUID | None = Header(None, alias="Last-Event-ID"),
    principal: Principal = Depends(current_principal),
) -> StreamingResponse:
    """
    Mốc replay ưu tiên header `Last-Event-ID` hơn query param: khi EventSource
    TỰ nối lại, nó gửi header này với event cuối cùng thực sự nhận được, nhưng
    giữ nguyên URL cũ — tức `last_request_id` trên URL là mốc của lần mở ĐẦU
    TIÊN, đã cũ.
    """
    if hub.connection_count(principal.user_id) >= settings.sse_max_connections_per_user:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Quá nhiều kết nối SSE đang mở",
        )
    metrics.incr("sse_connections")
    return StreamingResponse(
        event_stream(request, principal.user_id, last_event_id or last_request_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tắt buffer của nginx, nếu không nginx sẽ gom event lại rồi mới
            # đẩy một lượt và SSE mất hết tính realtime.
            "X-Accel-Buffering": "no",
        },
    )
