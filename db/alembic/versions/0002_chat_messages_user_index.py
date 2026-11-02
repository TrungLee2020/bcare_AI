"""Index chat_messages(user_id, id) cho GET /v1/conversations.

Postgres: CONCURRENTLY để không khoá ghi lịch sử trong lúc tạo. Lần trước bị
ngắt giữa chừng thì Postgres để lại index INVALID mà IF NOT EXISTS vẫn coi là
có, nên xoá nó trước rồi tạo lại.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

INDEX = "idx_chat_messages_user_id"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        op.create_index(INDEX, "chat_messages", ["user_id", "id"], if_not_exists=True)
        return
    with op.get_context().autocommit_block():
        invalid = bind.scalar(
            sa.text(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = :name AND NOT i.indisvalid"
            ),
            {"name": INDEX},
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
        op.create_index(
            INDEX, "chat_messages", ["user_id", "id"], postgresql_concurrently=True, if_not_exists=True
        )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="chat_messages")
