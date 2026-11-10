"""health_documents + OCR + bản trích xuất: ảnh giấy tờ y tế gửi qua chat.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

JsonType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "health_documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("conversation_id", sa.Uuid()),
        sa.Column("profile_id", sa.String(64)),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("doc_type", sa.String(20)),
        sa.Column("confidence", sa.Float()),
        sa.Column("title", sa.Text()),
        sa.Column("doc_date", sa.Date()),
        sa.Column("meta", JsonType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('received', 'processing', 'done', 'skipped', 'unreadable', 'quota_exceeded', 'failed')",
            name="ck_health_documents_status",
        ),
    )
    op.create_index(
        "idx_health_documents_user_created", "health_documents", ["user_id", sa.text("created_at DESC")]
    )
    op.create_index("idx_health_documents_conversation", "health_documents", ["conversation_id"])
    op.create_index(
        "uq_health_documents_user_sha256", "health_documents", ["user_id", "sha256"], unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"), sqlite_where=sa.text("deleted_at IS NULL"),
    )
    op.create_table(
        "health_document_ocr",
        sa.Column(
            "document_id", sa.Uuid(), sa.ForeignKey("health_documents.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "health_document_extractions",
        sa.Column(
            "id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), primary_key=True, autoincrement=True
        ),
        sa.Column(
            "document_id", sa.Uuid(), sa.ForeignKey("health_documents.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("schema_version", sa.String(16), nullable=False),
        sa.Column("data", JsonType, nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "idx_health_document_extractions_document", "health_document_extractions", ["document_id", "id"]
    )


def downgrade() -> None:
    op.drop_table("health_document_extractions")
    op.drop_table("health_document_ocr")
    op.drop_table("health_documents")
