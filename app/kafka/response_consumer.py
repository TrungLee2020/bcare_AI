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

Không dùng consumer group (group_id=None): trước đây mỗi lần khởi động lại sinh
một group tên ngẫu nhiên, commit offset, rồi bỏ đó — mỗi lần deploy để lại vài
group rác trong Kafka. Instance này chỉ cần đọc từ "bây giờ" và không bao giờ
đọc lại, nên không có gì để commit.

Bộ đệm replay KHÔNG ghi ở đây mà ở consumer của chat_requests (xem
`app.kafka.consumer._deliver`): ghi ở đây là mỗi instance ghi một lần.
"""

import asyncio
import logging

from aiokafka import AIOKafkaConsumer
from pydantic import ValidationError

from app.config import settings
from app.schemas import ChatResponseMessage
from app.sse import hub

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None

RESTART_DELAY_SECONDS = 2.0


async def _loop_once() -> None:
    consumer = AIOKafkaConsumer(
        settings.kafka_topic_chat_responses,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=None,
        enable_auto_commit=False,
        # Chỉ quan tâm response phát ra TỪ GIỜ; phần đã lỡ được lo bằng replay
        # buffer.
        auto_offset_reset="latest",
    )
    await consumer.start()
    logger.info("SSE response consumer started")
    try:
        async for record in consumer:
            try:
                response = ChatResponseMessage.model_validate_json(record.value)
            except ValidationError:
                logger.exception("Response sai schema tại offset=%s, bỏ qua", record.offset)
                continue

            delivered = hub.publish(response)
            logger.debug(
                "Response request_id=%s -> %d kết nối SSE", response.request_id, delivered
            )
    finally:
        await consumer.stop()
        logger.info("SSE response consumer stopped")


async def _loop() -> None:
    """Tự khởi động lại khi lỗi: chết ở đây là instance này im lặng không đẩy
    thêm câu trả lời nào xuống SSE nữa, trong khi /health vẫn báo ổn."""
    while True:
        try:
            await _loop_once()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "SSE response consumer lỗi, khởi động lại sau %.0fs", RESTART_DELAY_SECONDS
            )
            await asyncio.sleep(RESTART_DELAY_SECONDS)


def is_alive() -> bool:
    return _task is not None and not _task.done()


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
