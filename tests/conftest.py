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


@pytest_asyncio.fixture
async def db_maker():
    """
    SQLite in-memory qua aiosqlite: test chạy trên SQL thật (transaction, khoá
    ngoại, unique index đều có hiệu lực) mà không cần dựng Postgres trong CI.

    Khác biệt so với Postgres cần biết: JSONB -> JSON thường, BIGSERIAL ->
    INTEGER autoincrement. Các truy vấn ở repository đều là SQL chuẩn nên hành
    vi giống nhau, nhưng migration thật vẫn phải kiểm trên Postgres.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.models import Base
    from app.db.session import set_sessionmaker

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys=ON"))
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    set_sessionmaker(maker)
    yield maker
    set_sessionmaker(None)
    await engine.dispose()
