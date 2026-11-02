"""
Môi trường Alembic. Model trong app/db/models.py là đích; mỗi thay đổi model
đi kèm một revision trong versions/ (tests/test_migrations.py bắt lệch).

    alembic upgrade head                              # áp mọi revision còn thiếu
    alembic revision --autogenerate -m "mô tả ngắn"   # sinh revision từ model, đọc lại trước khi commit
"""

import asyncio
import logging.config

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    logging.config.fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _run(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


def main() -> None:
    # Test truyền sẵn connection (sync) để chạy trên DB của nó.
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    from app.config import settings

    asyncio.run(_run_async(settings.database_url))


if context.is_offline_mode():
    raise SystemExit("Không hỗ trợ chế độ offline (--sql): chạy thẳng trên DB.")
main()
