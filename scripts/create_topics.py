"""
Tạo các topic Kafka cần thiết cho môi trường dev/local.

Production nên tạo topic bằng IaC/admin tool của hạ tầng, không chạy script này.
Điểm quan trọng: `chat_requests` phải có NHIỀU HƠN 1 partition, nếu không thì
mọi user dồn vào 1 partition và mất hết khả năng xử lý song song.

    python -m scripts.create_topics
"""

import asyncio
import logging

from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from app.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("create_topics")


async def main() -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap_servers)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        wanted = [
            NewTopic(
                name=name,
                num_partitions=settings.kafka_num_partitions,
                replication_factor=settings.kafka_replication_factor,
            )
            for name in (
                settings.kafka_topic_chat_requests,
                settings.kafka_topic_chat_responses,
            )
            if name not in existing
        ]
        if not wanted:
            logger.info("Tất cả topic đã tồn tại, không tạo thêm")
            return
        await admin.create_topics(wanted)
        for topic in wanted:
            logger.info(
                "Đã tạo topic %s (partitions=%d)", topic.name, topic.num_partitions
            )
    finally:
        await admin.close()


if __name__ == "__main__":
    asyncio.run(main())
