import logging

from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger(__name__)

_redis: Redis | None = None

REDIS_TIMEOUT_SECONDS = 5.0


async def start_redis() -> None:
    global _redis
    # Có timeout: Redis treo (không phải chết hẳn) thì request/consumer báo lỗi
    # sau vài giây, thay vì treo theo vô thời hạn.
    _redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=REDIS_TIMEOUT_SECONDS,
        socket_connect_timeout=REDIS_TIMEOUT_SECONDS,
    )
    await _redis.ping()
    # Không log nguyên URL: nó chứa mật khẩu.
    kwargs = _redis.connection_pool.connection_kwargs
    logger.info("Redis connected (%s:%s)", kwargs.get("host"), kwargs.get("port"))


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
