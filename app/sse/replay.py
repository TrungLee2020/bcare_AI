"""
Bộ đệm response gần nhất theo user, để client reconnect lấy lại phần đã lỡ.

Mất mạng giữa chừng là chuyện bình thường trên mobile. Không có bộ đệm này thì
câu trả lời phát ra trong lúc client offline sẽ mất hẳn: message Kafka đã được
consume và commit, không ai phát lại nữa.
"""

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from redis.asyncio import Redis

from app.config import settings
from app.schemas import ChatResponseMessage

logger = logging.getLogger(__name__)


def _key(user_id: int) -> str:
    return f"stream:{user_id}"


async def remember(redis: Redis, response: ChatResponseMessage) -> None:
    """Lưu response vào bộ đệm. Bỏ qua `processing` — đó chỉ là tín hiệu giữ
    nhịp, phát lại một thông báo "đang xử lý" đã cũ chỉ gây rối cho client."""
    if response.status == "processing":
        return

    key = _key(response.user_id)
    pipe = redis.pipeline()
    pipe.lpush(key, response.model_dump_json())
    pipe.ltrim(key, 0, settings.sse_replay_buffer_size - 1)
    pipe.expire(key, settings.sse_replay_ttl_seconds)
    await pipe.execute()


async def missed_since(
    redis: Redis, user_id: int, last_request_id: UUID | str | None
) -> list[ChatResponseMessage]:
    """
    Các response phát ra SAU `last_request_id`, theo thứ tự cũ -> mới.
    Không có `last_request_id` (kết nối mới): các response của
    `sse_replay_on_connect_seconds` giây gần nhất.

    Không tìm thấy `last_request_id` trong bộ đệm (client offline quá lâu, hoặc
    id lạ) thì trả về TOÀN BỘ bộ đệm: thà gửi thừa còn hơn để mất câu trả lời.
    Client dedup được bằng request_id.
    """
    raw = await redis.lrange(_key(user_id), 0, -1)  # mới -> cũ
    items = [ChatResponseMessage.model_validate_json(r) for r in reversed(raw)]
    if last_request_id is None:
        # Kết nối mới: chỉ phát lại phần vừa phát trong vài giây gần đây — đủ
        # để câu trả lời tới trước khi SSE kịp mở không bị mất, mà không phát
        # lại cả lịch sử mỗi lần mở trang.
        since = datetime.now(timezone.utc) - timedelta(
            seconds=settings.sse_replay_on_connect_seconds
        )
        return [item for item in items if item.created_at >= since]

    wanted = str(last_request_id)
    for index, item in enumerate(items):
        if str(item.request_id) == wanted:
            return items[index + 1 :]
    logger.info(
        "Không thấy last_request_id=%s trong bộ đệm của user_id=%s, phát lại toàn bộ",
        wanted,
        user_id,
    )
    return items
