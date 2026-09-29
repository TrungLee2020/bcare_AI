"""
Xoá SẠCH dữ liệu của môi trường test: topic Kafka, key của app trong Redis,
lịch sử chat trong Postgres. Dùng khi chuyển một cụm hạ tầng đang chạy thử sang
dùng với người dùng thật.

    python -m scripts.reset_test_data            # chỉ in ra sẽ xoá gì
    python -m scripts.reset_test_data --yes      # xoá thật

Vì sao cần: dữ liệu test còn sót sẽ lẫn vào dữ liệu thật —
- Kafka: consumer group mới đọc từ đầu topic (auto_offset_reset=earliest).
  Consumer đã bỏ qua câu hỏi quá REQUEST_MAX_AGE_SECONDS, nhưng topic vẫn giữ
  nội dung câu hỏi test tới hết retention.
- Redis: counter quota, cache câu trả lời, bộ đệm replay SSE của user_id test.
  user_id test trùng user_id thật là user thật mất lượt hỏi / nhận câu trả lời
  của người khác khi reconnect SSE.
- Postgres: lịch sử chat của session test.

Từ chối chạy khi APP_ENV=production. Chỉ xoá key có tiền tố của app trong Redis
(không FLUSHDB), vì Redis có thể dùng chung với dịch vụ khác.
"""

import argparse
import asyncio
import logging

from aiokafka.admin import AIOKafkaAdminClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.kafka.topics import retention_ms, topic_specs

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("reset_test_data")

# Mọi key app ghi vào Redis (quota, idempotency, replay SSE)
REDIS_PREFIXES = ("quota:", "dedup:", "resp:", "processed:", "stream:")


async def reset_kafka() -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=settings.kafka_bootstrap_servers)
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        doomed = [name for name in retention_ms() if name in existing]
        if doomed:
            await admin.delete_topics(doomed)
            logger.info("Đã xoá topic: %s", ", ".join(doomed))
            # Kafka xoá topic bất đồng bộ; tạo lại ngay có thể báo "đang bị xoá"
            await asyncio.sleep(5)
        await admin.create_topics(topic_specs())
        logger.info("Đã tạo lại topic theo cấu hình hiện tại")
    finally:
        await admin.close()


async def reset_redis() -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        deleted = 0
        for prefix in REDIS_PREFIXES:
            batch = []
            async for key in redis.scan_iter(match=f"{prefix}*", count=1000):
                batch.append(key)
                if len(batch) >= 1000:
                    deleted += await redis.delete(*batch)
                    batch = []
            if batch:
                deleted += await redis.delete(*batch)
        logger.info("Đã xoá %d key Redis (%s)", deleted, ", ".join(REDIS_PREFIXES))
    finally:
        await redis.aclose()


async def reset_postgres() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE chat_messages, chat_sessions RESTART IDENTITY"))
        logger.info("Đã xoá lịch sử chat (chat_messages, chat_sessions)")
    finally:
        await engine.dispose()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--yes", action="store_true", help="xoá thật")
    args = parser.parse_args()

    if settings.app_env == "production":
        raise SystemExit("APP_ENV=production: từ chối xoá dữ liệu.")

    logger.info("Kafka:    %s -> topic %s", settings.kafka_bootstrap_servers, list(retention_ms()))
    logger.info("Redis:    %s -> key %s", settings.redis_url, REDIS_PREFIXES)
    logger.info("Postgres: %s -> chat_messages, chat_sessions", settings.database_url.split("@")[-1])
    if not args.yes:
        logger.info("Chưa xoá gì. Chạy lại với --yes để xoá thật. Nhớ tắt app trước.")
        return

    await reset_kafka()
    await reset_redis()
    await reset_postgres()
    logger.info("Xong. Khởi động lại app.")


if __name__ == "__main__":
    asyncio.run(main())
