"""Revision Alembic phải dựng ra đúng schema của app/db/models.py, cả trên DB
mới lẫn DB production đã có bảng từ trước khi dùng Alembic.

Mặc định chạy trên SQLite; đặt TEST_DATABASE_URL để chạy trên Postgres thật
(nhánh CONCURRENTLY của 0002 chỉ chạy ở đó)."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.models import Base, ChatMessage, ChatSession

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
async def conn(tmp_path):
    url = os.getenv("TEST_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'migrate.db'}")
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(_reset)
        yield connection
    await engine.dispose()


def _reset(sync_conn) -> None:
    Base.metadata.drop_all(sync_conn)
    sync_conn.execute(sa.text("DROP TABLE IF EXISTS alembic_version"))
    sync_conn.commit()


def _upgrade(sync_conn, target: str = "head") -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["connection"] = sync_conn
    command.upgrade(config, target)
    sync_conn.commit()


def _diff(sync_conn) -> list:
    return compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)


def _indexes(sync_conn, table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(sync_conn).get_indexes(table)}


async def test_db_moi_upgrade_ra_dung_schema_cua_model(conn):
    """Sửa model mà quên `alembic revision --autogenerate` thì test này đỏ."""
    await conn.run_sync(_upgrade)
    assert await conn.run_sync(_diff) == []


async def test_db_production_cu_duoc_nang_cap_ma_giu_du_lieu(conn):
    """Production: bảng tạo bằng db/schema.sql cũ, chưa có alembic_version,
    chưa có index user_id."""

    def prepare(sync_conn):
        Base.metadata.create_all(sync_conn)
        sync_conn.execute(sa.text("DROP INDEX idx_chat_messages_user_id"))
        sid = uuid4()
        sync_conn.execute(sa.insert(ChatSession).values(id=sid, user_id="u1"))
        sync_conn.execute(sa.insert(ChatMessage).values(
            session_id=sid, user_id="u1", request_id=uuid4(), role="user", content="câu cũ", meta={},
        ))
        sync_conn.commit()

    await conn.run_sync(prepare)
    await conn.run_sync(_upgrade)

    assert "idx_chat_messages_user_id" in await conn.run_sync(_indexes, "chat_messages")
    assert await conn.run_sync(_diff) == []
    content = await conn.scalar(sa.select(ChatMessage.content))
    assert content == "câu cũ"


async def test_upgrade_lan_hai_khong_lam_gi(conn):
    await conn.run_sync(_upgrade)
    await conn.run_sync(_upgrade)
    version = await conn.scalar(sa.text("SELECT version_num FROM alembic_version"))
    assert version == "0002"
