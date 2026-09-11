import fakeredis.aioredis
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def _reset_settings():
    """Giữ quota mặc định (free=2, premium=5) ổn định giữa các test."""
    from app.config import settings

    before = (settings.quota_free_per_day, settings.quota_premium_per_day)
    yield
    settings.quota_free_per_day, settings.quota_premium_per_day = before
