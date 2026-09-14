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
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app import metrics
from app.auth import Principal, current_principal

from app.config import settings
from app.redis_client import get_redis
from app.schemas import ChatResponseMessage
from app.sse import hub, replay

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])


def format_event(response: ChatResponseMessage) -> str:
    """
    Một khung SSE. `id:` để client tự nhớ mốc cuối cùng nhận được và gửi lại
    khi reconnect.
    """
    payload = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
    return f"id: {response.request_id}\nevent: {response.status}\ndata: {payload}\n\n"


async def event_stream(request: Request, user_id: int, last_request_id: UUID | None):
    queue = hub.subscribe(user_id)
    try:
        for missed in await replay.missed_since(get_redis(), user_id, last_request_id):
            yield format_event(missed)

        while True:
            if await request.is_disconnected():
                break
            try:
                response = await asyncio.wait_for(
                    queue.get(), timeout=settings.sse_heartbeat_seconds
                )
            except TimeoutError:
                # Comment rỗng: proxy/LB thường đóng kết nối idle sau 30-60s.
                yield ": keepalive\n\n"
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
    principal: Principal = Depends(current_principal),
) -> StreamingResponse:
    metrics.incr("sse_connections")
    return StreamingResponse(
        event_stream(request, principal.user_id, last_request_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Tắt buffer của nginx, nếu không nginx sẽ gom event lại rồi mới
            # đẩy một lượt và SSE mất hết tính realtime.
            "X-Accel-Buffering": "no",
        },
    )
