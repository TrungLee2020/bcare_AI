"""
Đọc topic `chat_responses` và đẩy vào hub SSE của instance này.

**Mỗi instance dùng một consumer group RIÊNG** (gắn thêm id ngẫu nhiên). Đây là
điểm khác hẳn consumer của `chat_requests`:

- `chat_requests` cần CHIA việc: mỗi message chỉ được 1 instance xử lý, nên tất
  cả instance dùng CHUNG một group.
- `chat_responses` cần PHÁT TỚI TẤT CẢ: kết nối SSE của user đang nằm ở đúng 1
  instance cụ thể, và không instance nào biết trước là instance nào. Nếu dùng
  chung group, response có thể rơi vào instance không giữ kết nối của user đó
  và câu trả lời sẽ không bao giờ tới nơi.

Cái giá: mọi instance đều đọc mọi response. Chấp nhận được ở quy mô hiện tại
(response nhỏ, lượng thấp). Khi cần tiết kiệm hơn thì chuyển sang định tuyến
dính (sticky routing theo user_id) hoặc fanout qua Redis pub/sub.
"""

import asyncio
import logging
import uuid

from aiokafka import AIOKafkaConsumer
from pydantic import ValidationError

from app.config import settings
from app.redis_client import get_redis
from app.schemas import ChatResponseMessage
from app.sse import hub, replay

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _loop() -> None:
    group = f"{settings.kafka_consumer_group}-sse-{uuid.uuid4().hex[:8]}"
    consumer = AIOKafkaConsumer(
        settings.kafka_topic_chat_responses,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=group,
        enable_auto_commit=True,
        # Chỉ quan tâm response phát ra TỪ GIỜ. Group này sinh ra cùng process
        # và chết cùng process, đọc lại từ đầu topic là vô nghĩa (toàn response
        # cũ của những kết nối đã đóng) — phần đã lỡ được lo bằng replay buffer.
        auto_offset_reset="latest",
    )
    await consumer.start()
    logger.info("SSE response consumer started (group=%s)", group)
    try:
        async for record in consumer:
            try:
                response = ChatResponseMessage.model_validate_json(record.value)
            except ValidationError:
                logger.exception("Response sai schema tại offset=%s, bỏ qua", record.offset)
                continue

            await replay.remember(get_redis(), response)
            delivered = hub.publish(response)
            logger.debug(
                "Response request_id=%s -> %d kết nối SSE", response.request_id, delivered
            )
    finally:
        await consumer.stop()
        logger.info("SSE response consumer stopped")


def start_response_consumer() -> None:
    global _task
    _task = asyncio.create_task(_loop())


async def stop_response_consumer() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
