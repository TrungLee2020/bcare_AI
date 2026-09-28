"""
Tạo các topic Kafka theo cấu hình ở `app/kafka/topics.py`.

    python -m scripts.create_topics

Production: đặt KAFKA_REPLICATION_FACTOR=3 và KAFKA_MIN_INSYNC_REPLICAS=2 trước
khi chạy. Với APP_ENV=production script từ chối tạo topic ít bản sao hơn — và
app cũng từ chối khởi động nếu topic không đạt (xem verify_production_topics).

Topic đã tồn tại thì KHÔNG bị sửa: đổi số partition hay số bản sao của topic
đang chạy là thao tác vận hành phải làm có chủ đích, không phải việc của script.
"""

import asyncio
import logging

from aiokafka.admin import AIOKafkaAdminClient

from app.config import settings
from app.kafka.topics import layout_problems, topic_specs

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("create_topics")


async def main() -> None:
    if settings.app_env == "production" and (
        settings.kafka_replication_factor < 3 or settings.kafka_min_insync_replicas < 2
    ):
        raise SystemExit(
            "APP_ENV=production cần KAFKA_REPLICATION_FACTOR>=3 và "
            "KAFKA_MIN_INSYNC_REPLICAS>=2"
        )

    admin = AIOKafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap_servers)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        wanted = [t for t in topic_specs() if t.name not in existing]
        if wanted:
            await admin.create_topics(wanted)
            for topic in wanted:
                logger.info(
                    "Đã tạo topic %s (partitions=%d, replication=%d, configs=%s)",
                    topic.name,
                    topic.num_partitions,
                    topic.replication_factor,
                    topic.topic_configs,
                )
        else:
            logger.info("Tất cả topic đã tồn tại, không tạo thêm")

        described = await admin.describe_topics([t.name for t in topic_specs()])
        for problem in layout_problems(described, settings.kafka_replication_factor):
            logger.warning("Topic có sẵn không khớp cấu hình: %s", problem)
    finally:
        await admin.close()


if __name__ == "__main__":
    asyncio.run(main())
