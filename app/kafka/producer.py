import logging

from aiokafka import AIOKafkaProducer

from app.config import settings
from app.schemas import ChatRequestMessage

logger = logging.getLogger(__name__)

_producer: AIOKafkaProducer | None = None


async def start_producer() -> None:
    global _producer
    _producer = AIOKafkaProducer(
        bootstrap_servers=settings.kafka_bootstrap_servers,
        # key mặc định là bytes; ta tự encode ở publish_chat_request
        acks="all",  # đợi broker ack đầy đủ, ưu tiên không mất message hơn latency
        enable_idempotence=True,  # tránh duplicate ở tầng producer nếu retry nội bộ
    )
    await _producer.start()
    logger.info("Kafka producer started (bootstrap=%s)", settings.kafka_bootstrap_servers)


async def stop_producer() -> None:
    global _producer
    if _producer is not None:
        await _producer.stop()
        _producer = None
        logger.info("Kafka producer stopped")


async def publish_chat_request(message: ChatRequestMessage) -> None:
    """
    Publish 1 chat request lên topic chat_requests.
    Key = user_id -> đảm bảo cùng user luôn rơi vào cùng 1 partition (giữ thứ tự).
    """
    if _producer is None:
        raise RuntimeError("Producer chưa được start (gọi start_producer() trước)")

    key = str(message.user_id).encode("utf-8")
    value = message.model_dump_json().encode("utf-8")

    result = await _producer.send_and_wait(
        settings.kafka_topic_chat_requests, value=value, key=key
    )
    logger.info(
        "Published request_id=%s user_id=%s -> partition=%s offset=%s",
        message.request_id,
        message.user_id,
        result.partition,
        result.offset,
    )
