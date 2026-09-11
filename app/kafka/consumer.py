import asyncio
import logging

from aiokafka import AIOKafkaConsumer
from pydantic import ValidationError

from app.config import settings
from app.schemas import ChatRequestMessage

logger = logging.getLogger(__name__)

_consumer_task: asyncio.Task | None = None
_stop_event = asyncio.Event()


async def _consume_loop() -> None:
    """
    Phase 1 skeleton: KHÔNG gọi OpenAI ở đây.
    Mục tiêu duy nhất của bước này là verify:
      - Message tới đúng thứ tự trong cùng 1 partition (tức cùng 1 user_id).
      - Consumer group hoạt động đúng khi có nhiều instance (rebalance ổn định).
      - Offset chỉ được commit SAU khi xử lý xong (ở đây là "xử lý" = log),
        để không bao giờ mất message nếu consumer crash giữa chừng.

    enable_auto_commit=False + commit thủ công sau mỗi message là chủ đích,
    không phải quên tắt — tự động commit có thể ack message trước khi biết
    chắc đã xử lý xong, dẫn tới mất message khi consumer crash.
    """
    consumer = AIOKafkaConsumer(
        settings.kafka_topic_chat_requests,
        bootstrap_servers=settings.kafka_bootstrap_servers,
        group_id=settings.kafka_consumer_group,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    await consumer.start()
    logger.info(
        "Consumer started: topic=%s group=%s",
        settings.kafka_topic_chat_requests,
        settings.kafka_consumer_group,
    )

    try:
        async for record in consumer:
            if _stop_event.is_set():
                break

            try:
                message = ChatRequestMessage.model_validate_json(record.value)
                logger.info(
                    "Consumed request_id=%s user_id=%s partition=%s offset=%s "
                    "key=%s content_len=%d",
                    message.request_id,
                    message.user_id,
                    record.partition,
                    record.offset,
                    record.key,
                    len(message.content),
                )
                # TODO Phase 2: check quota + idempotency trước khi xử lý tiếp
                # TODO Phase 3: gọi OpenAI ở đây, publish kết quả sang chat_responses
            except ValidationError:
                # Message sai schema không nên làm chết cả consumer loop.
                # Ở Phase 5 sẽ đẩy các message lỗi này sang 1 dead-letter topic
                # thay vì chỉ log rồi bỏ qua như hiện tại.
                logger.exception(
                    "Message tại partition=%s offset=%s không đúng schema, bỏ qua",
                    record.partition,
                    record.offset,
                )

            # "Ack" = commit offset. Làm sau khi xử lý (kể cả khi lỗi schema ở trên)
            # để tránh consumer bị kẹt lặp lại mãi 1 message hỏng.
            await consumer.commit()
    finally:
        await consumer.stop()
        logger.info("Consumer stopped")


def start_consumer() -> None:
    global _consumer_task
    _stop_event.clear()
    _consumer_task = asyncio.create_task(_consume_loop())


async def stop_consumer() -> None:
    global _consumer_task
    _stop_event.set()
    if _consumer_task is not None:
        await _consumer_task
        _consumer_task = None
