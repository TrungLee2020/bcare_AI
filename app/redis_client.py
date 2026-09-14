import logging

from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

_redis: Redis | None = None


async def start_redis() -> None:
    global _redis
    _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    await _redis.ping()
    logger.info("Redis connected (%s)", settings.redis_url)


async def stop_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        logger.info("Redis connection closed")


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("Redis chưa được start (gọi start_redis() trước)")
    return _redis


def set_redis(client: Redis | None) -> None:
    """Dùng cho test: inject fakeredis mà không cần chạy Redis thật."""
    global _redis
    _redis = client
