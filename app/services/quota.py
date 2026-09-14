from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from redis.asyncio import Redis

from app.config import settings

# Check + increment phải nằm trong 1 script Lua: nếu tách thành GET rồi INCR ở
# phía Python thì 2 request đồng thời của cùng 1 user đều đọc được used=1 và
# cùng đi qua, user free hỏi được 3 câu thay vì 2 (race condition mà Phase 6
# yêu cầu test concurrent increment).
_CONSUME_LUA = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
local limit = tonumber(ARGV[1])
if used >= limit then
  return {0, used}
end
used = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return {1, used}
"""

# Hoàn quota khi enqueue thất bại. Không dùng DECR trần vì nếu key đã hết hạn
# (sang ngày mới) thì DECR sẽ tạo key mới với giá trị -1, làm user ngày hôm sau
# được cộng thêm 1 lượt miễn phí.
_REFUND_LUA = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
if used > 0 then
  return redis.call('DECR', KEYS[1])
end
return 0
"""


class QuotaExceeded(Exception):
    def __init__(self, tier: str, limit: int):
        self.tier = tier
        self.limit = limit
        super().__init__(
            f"Bạn đã hết lượt hỏi hôm nay ({tier}: {limit} câu/ngày). "
            f"Vui lòng thử lại vào ngày mai."
        )


def _now() -> datetime:
    return datetime.now(ZoneInfo(settings.quota_timezone))


def quota_key(user_id: int, now: datetime | None = None) -> str:
    now = now or _now()
    return f"quota:{user_id}:{now:%Y-%m-%d}"


def seconds_until_midnight(now: datetime | None = None) -> int:
    """TTL của counter = thời gian còn lại tới nửa đêm giờ VN, +60s đệm để
    tránh key hết hạn sớm hơn mốc reset do lệch đồng hồ giữa app và Redis."""
    now = now or _now()
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return int((midnight - now).total_seconds()) + 60


async def consume(redis: Redis, user_id: int, tier: str) -> int:
    """
    Trừ 1 lượt hỏi của user. Trả về số lượt CÒN LẠI trong ngày.
    Raise QuotaExceeded nếu đã hết lượt (và không trừ thêm gì).
    """
    limit = settings.quota_limit_for_tier(tier)
    ok, used = await redis.eval(
        _CONSUME_LUA, 1, quota_key(user_id), limit, seconds_until_midnight()
    )
    if not int(ok):
        raise QuotaExceeded(tier, limit)
    return limit - int(used)


async def refund(redis: Redis, user_id: int) -> None:
    """Trả lại 1 lượt đã trừ (dùng khi enqueue Kafka lỗi -> user chưa hỏi được gì)."""
    await redis.eval(_REFUND_LUA, 1, quota_key(user_id))


async def remaining(redis: Redis, user_id: int, tier: str) -> int:
    limit = settings.quota_limit_for_tier(tier)
    used = await redis.get(quota_key(user_id))
    return limit - int(used or 0)
