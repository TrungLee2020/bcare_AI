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
    """Giữ config ổn định giữa các test, và đặt sẵn secret + mở rollout 100%
    để test không phải lo hai thứ đó ở mọi chỗ."""
    from app import metrics
    from app.config import settings

    keys = (
        "quota_free_per_day", "quota_premium_per_day", "auth_secret",
        "auth_required", "rollout_enabled", "rollout_percentage",
        "rollout_allowlist",
    )
    before = {k: getattr(settings, k) for k in keys}
    settings.auth_secret = "test-secret"
    settings.auth_required = True
    settings.rollout_enabled = True
    settings.rollout_percentage = 100
    settings.rollout_allowlist = ""
    metrics.reset()
    yield
    for k, v in before.items():
        setattr(settings, k, v)


@pytest_asyncio.fixture
async def db_maker():
    """
    Mặc định: SQLite in-memory qua aiosqlite — test chạy trên SQL thật
    (transaction, khoá ngoại, unique index đều có hiệu lực) mà CI không cần
    dựng Postgres.

    Đặt `TEST_DATABASE_URL` để chạy đúng bộ test đó trên Postgres thật:

        TEST_DATABASE_URL=postgresql+asyncpg://bcare@/bcare?host=/tmp pytest

    Cần chạy trên Postgres trước khi rollout, vì SQLite khác Postgres ở đúng
    những chỗ schema đang dùng: JSONB -> JSON, BIGSERIAL -> INTEGER.
    """
    import os

    import sqlalchemy
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.models import Base
    from app.db.session import set_sessionmaker

    url = os.getenv("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            await conn.execute(sqlalchemy.text("PRAGMA foreign_keys=ON"))
        else:
            # Postgres: dọn sạch giữa các test vì DB không phải in-memory
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    set_sessionmaker(maker)
    yield maker
    set_sessionmaker(None)
    await engine.dispose()
