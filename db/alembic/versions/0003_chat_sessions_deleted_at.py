"""chat_sessions.deleted_at: user xoá hội thoại thì ẩn ngay, job xoá hẳn sau.

Thêm cột nullable không default: Postgres chỉ sửa catalog, không viết lại bảng.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("deleted_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("chat_sessions", "deleted_at")
