"""Baseline: chat_sessions, chat_messages như trước khi dùng Alembic.

DB production đã có sẵn các bảng này (tạo bằng db/schema.sql cũ), nên bảng
nào có rồi thì bỏ qua: lần `upgrade` đầu trên production chỉ ghi version, DB
mới thì tạo đủ.

Revision ID: 0001
Revises:
Create Date: 2026-11-02
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())

    if "chat_sessions" not in existing:
        op.create_table(
            "chat_sessions",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("summary", sa.Text()),
            sa.Column("summarized_through_id", sa.BigInteger()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index(
            "idx_chat_sessions_user_created", "chat_sessions", ["user_id", sa.text("created_at DESC")]
        )

    if "chat_messages" not in existing:
        op.create_table(
            "chat_messages",
            sa.Column(
                "id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
            ),
            sa.Column(
                "session_id", sa.Uuid(), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("request_id", sa.Uuid(), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("content", sa.Text(), nullable=False),
            sa.Column("meta", JsonType, nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_messages_role"),
        )
        op.create_index("uq_chat_messages_request_role", "chat_messages", ["request_id", "role"], unique=True)
        op.create_index("idx_chat_messages_session_id", "chat_messages", ["session_id", "id"])


def downgrade() -> None:
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
