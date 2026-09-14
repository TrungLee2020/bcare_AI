"""
Sổ đăng ký các kết nối SSE đang mở TRÊN CHÍNH INSTANCE NÀY.

Đây chỉ là bộ nhớ trong process — instance A không biết gì về kết nối của
instance B. Việc đưa response tới đúng instance đang giữ kết nối của user là
việc của `app/kafka/response_consumer.py` (mỗi instance đọc TẤT CẢ response).
"""

import asyncio
import logging
from collections import defaultdict

from app.schemas import ChatResponseMessage

logger = logging.getLogger(__name__)

# Hàng đợi có giới hạn: client chậm (mạng yếu, tab bị treo) không được phép làm
# phình bộ nhớ vô hạn. Đầy thì bỏ event CŨ NHẤT — client có thể lấy lại bằng
# cơ chế replay khi reconnect.
QUEUE_MAX_SIZE = 50

_subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)


def subscribe(user_id: int) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
    _subscribers[user_id].add(queue)
    logger.info(
        "SSE mở kết nối user_id=%s (đang có %d kết nối)", user_id, len(_subscribers[user_id])
    )
    return queue


def unsubscribe(user_id: int, queue: asyncio.Queue) -> None:
    _subscribers[user_id].discard(queue)
    if not _subscribers[user_id]:
        # Xoá hẳn key, nếu không dict sẽ phình theo tổng số user từng kết nối
        _subscribers.pop(user_id, None)


def connection_count(user_id: int) -> int:
    return len(_subscribers.get(user_id, ()))


def publish(response: ChatResponseMessage) -> int:
    """
    Đẩy 1 response tới mọi kết nối đang mở của user (user có thể mở nhiều tab).
    Trả về số kết nối đã nhận — 0 nghĩa là user hiện không online ở instance này.
    """
    queues = _subscribers.get(response.user_id, set())
    delivered = 0
    for queue in list(queues):
        try:
            queue.put_nowait(response)
            delivered += 1
        except asyncio.QueueFull:
            try:
                queue.get_nowait()  # bỏ event cũ nhất, nhường chỗ cho event mới
                queue.put_nowait(response)
                delivered += 1
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                logger.warning("Hàng đợi SSE của user_id=%s bị nghẽn", response.user_id)
    return delivered


def reset() -> None:
    """Dùng cho test."""
    _subscribers.clear()
