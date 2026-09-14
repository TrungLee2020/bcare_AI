import asyncio

import pytest

from app.config import settings
from app.services import quota


async def test_free_tier_het_luot_sau_2_cau(redis):
    assert await quota.consume(redis, user_id=1, tier="free") == 1
    assert await quota.consume(redis, user_id=1, tier="free") == 0

    with pytest.raises(quota.QuotaExceeded) as exc:
        await quota.consume(redis, user_id=1, tier="free")
    assert "hết lượt hỏi hôm nay" in str(exc.value)


async def test_premium_duoc_nhieu_luot_hon(redis):
    for _ in range(settings.quota_premium_per_day):
        await quota.consume(redis, user_id=2, tier="premium")
    with pytest.raises(quota.QuotaExceeded):
        await quota.consume(redis, user_id=2, tier="premium")


async def test_quota_tach_biet_giua_cac_user(redis):
    await quota.consume(redis, user_id=1, tier="free")
    await quota.consume(redis, user_id=1, tier="free")
    # user 2 không bị ảnh hưởng bởi việc user 1 đã dùng hết
    assert await quota.consume(redis, user_id=2, tier="free") == 1


async def test_concurrent_khong_vuot_limit(redis):
    """Phase 6 yêu cầu: 20 request đồng thời của cùng 1 user free chỉ được
    đúng 2 cái đi qua, phần còn lại phải bị chặn (không race)."""
    results = await asyncio.gather(
        *(quota.consume(redis, user_id=7, tier="free") for _ in range(20)),
        return_exceptions=True,
    )
    passed = [r for r in results if not isinstance(r, Exception)]
    blocked = [r for r in results if isinstance(r, quota.QuotaExceeded)]

    assert len(passed) == settings.quota_free_per_day
    assert len(blocked) == 20 - settings.quota_free_per_day
    assert sorted(passed) == [0, 1]  # số lượt còn lại sau mỗi lần trừ


async def test_refund_tra_lai_dung_1_luot(redis):
    await quota.consume(redis, user_id=3, tier="free")
    await quota.refund(redis, user_id=3)
    assert await quota.remaining(redis, user_id=3, tier="free") == 2


async def test_refund_khong_tao_quota_am_khi_key_da_het_han(redis):
    """Nếu key quota đã hết hạn (sang ngày mới) thì refund phải là no-op,
    không được để lại counter = -1 làm user hôm sau dư 1 lượt."""
    await quota.refund(redis, user_id=4)
    assert await quota.remaining(redis, user_id=4, tier="free") == 2


async def test_counter_co_ttl_de_tu_reset_sang_ngay_moi(redis):
    await quota.consume(redis, user_id=5, tier="free")
    ttl = await redis.ttl(quota.quota_key(5))
    assert 0 < ttl <= 24 * 3600 + 60
