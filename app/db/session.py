import logging
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

logger = logging.getLogger(__name__)

_engine = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


async def start_db() -> None:
    global _engine, _sessionmaker
    _engine = create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,  # tránh dùng lại connection đã chết sau khi DB restart
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    logger.info("Database pool sẵn sàng")


async def stop_db() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def set_sessionmaker(maker: async_sessionmaker[AsyncSession] | None) -> None:
    """Dùng cho test: trỏ sang SQLite in-memory thay vì Postgres thật."""
    global _sessionmaker
    _sessionmaker = maker


@asynccontextmanager
async def db_session():
    """
    Mở 1 transaction cho 1 đơn vị công việc. Commit khi thoát bình thường,
    rollback khi có exception.
    """
    if _sessionmaker is None:
        raise RuntimeError("Database chưa được start (gọi start_db() trước)")
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
