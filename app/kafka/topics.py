"""
Cấu hình các topic Kafka, và bước kiểm tra lúc khởi động ở production.

Một chỗ duy nhất định nghĩa topic trông như thế nào (số partition, số bản sao,
thời gian giữ), dùng chung cho `scripts/create_topics.py` và cho bước kiểm tra
khi app khởi động với APP_ENV=production.
"""

import logging

from aiokafka.admin import AIOKafkaAdminClient, NewTopic

from app.config import settings

logger = logging.getLogger(__name__)

HOUR_MS = 3600 * 1000
DAY_MS = 24 * HOUR_MS


def retention_ms() -> dict[str, int]:
    """
    Thời gian giữ message của từng topic.

    - `chat_requests`: 1 ngày. Câu hỏi quá `REQUEST_MAX_AGE_SECONDS` sẽ bị bỏ
      qua dù còn trong topic (xem consumer), nên giữ lâu hơn chỉ có ích cho
      điều tra. Giữ ngắn cũng để dữ liệu sức khoẻ không nằm trong Kafka lâu.
    - `chat_responses`: 1 giờ. Chỉ để đẩy xuống SSE; phần client lỡ được lấy
      lại qua bộ đệm replay trong Redis, không phải từ topic.
    - DLQ: 14 ngày, đủ để điều tra và replay.
    """
    return {
        settings.kafka_topic_chat_requests: DAY_MS,
        settings.kafka_topic_chat_responses: HOUR_MS,
        settings.kafka_topic_dead_letter: 14 * DAY_MS,
    }


def topic_specs() -> list[NewTopic]:
    return [
        NewTopic(
            name=name,
            num_partitions=settings.kafka_num_partitions,
            replication_factor=settings.kafka_replication_factor,
            topic_configs={
                "retention.ms": str(retention),
                # Với acks=all ở producer: ghi thành công nghĩa là ít nhất ngần
                # này bản sao đã có message. 1 broker chết không mất gì.
                "min.insync.replicas": str(settings.kafka_min_insync_replicas),
            },
        )
        for name, retention in retention_ms().items()
    ]


def layout_problems(described: list[dict], min_replication: int) -> list[str]:
    """
    Soi kết quả `describe_topics`: topic nào thiếu, partition nào có ít bản sao
    hơn `min_replication`. Tách riêng để test được không cần Kafka.
    """
    wanted = set(retention_ms())
    by_name = {t["topic"]: t for t in described}
    problems = []
    for name in sorted(wanted):
        topic = by_name.get(name)
        if topic is None or topic.get("error_code"):
            problems.append(f"topic {name} chưa tồn tại")
            continue
        partitions = topic.get("partitions") or []
        if not partitions:
            problems.append(f"topic {name} không có partition nào")
            continue
        replicas = min(len(p.get("replicas") or []) for p in partitions)
        if replicas < min_replication:
            problems.append(
                f"topic {name} có replication factor {replicas} < {min_replication}"
            )
    return problems


async def verify_production_topics() -> None:
    """
    Fail-closed lúc khởi động ở production: topic phải có sẵn và đủ bản sao.

    Replication factor 1 nghĩa là 1 broker chết là mất câu hỏi của user (và
    `acks=all` không giúp được gì, vì "all" lúc đó chỉ có 1). Lỗi này không
    hiện ra lúc chạy thử, chỉ hiện ra lúc có sự cố, nên phải chặn từ đầu.
    """
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap_servers)
    await admin.start()
    try:
        described = await admin.describe_topics(list(retention_ms()))
    finally:
        await admin.close()
    problems = layout_problems(described, min_replication=3)
    if problems:
        raise RuntimeError(
            "Kafka chưa sẵn sàng cho production: " + "; ".join(problems)
            + ". Tạo topic bằng: python -m scripts.create_topics "
            "(với KAFKA_REPLICATION_FACTOR=3, KAFKA_MIN_INSYNC_REPLICAS=2)"
        )
    logger.info("Kiểm tra topic Kafka cho production: đạt")
