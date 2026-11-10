"""
Model SQLAlchemy — nguồn sự thật DUY NHẤT cho schema.

Sửa model thì sinh kèm một revision Alembic rồi đọc lại trước khi commit:

    alembic revision --autogenerate -m "mô tả ngắn"

tests/test_migrations.py đỏ nếu model và các revision trong db/alembic lệch nhau.
"""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB trên Postgres, JSON thường trên SQLite (test chạy được không cần Postgres)
JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Tóm tắt các tin nhắn CŨ của phiên (sinh ở app/services/summarizer.py).
    # Có nó thì prompt chỉ cần chở summary + N tin gần nhất thay vì toàn bộ
    # lịch sử — vừa rẻ hơn vừa trả lời tốt hơn (prompt dài làm model loãng).
    summary: Mapped[str | None] = mapped_column(Text)
    # Mốc đã tóm tắt tới đâu: chỉ những message có id <= giá trị này mới nằm
    # trong summary. Thiếu mốc này thì không biết tin nào đã được tóm tắt, dẫn
    # tới vừa nhồi trùng vào prompt vừa tóm tắt lại từ đầu mỗi lần.
    summarized_through_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # User xoá hội thoại: ẩn ngay khỏi lịch sử và ngữ cảnh, job xoá hẳn khỏi
    # DB sau HISTORY_PURGE_AFTER_DAYS ngày (luật cho tối đa ~20 ngày).
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index("idx_chat_sessions_user_created", "user_id", created_at.desc()),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # request_id của message gốc từ Kafka. UNIQUE để idempotency không chỉ dựa
    # vào Redis: nếu Redis mất dữ liệu (restart, evict), ràng buộc này vẫn chặn
    # việc ghi trùng 1 lượt hỏi vào lịch sử.
    request_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # model, prompt_version, out_of_scope, refusal_reason... phục vụ debug và
    # phân tích chất lượng trả lời về sau
    meta: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="ck_chat_messages_role"),
        # (request_id, role) chứ không phải request_id đơn lẻ: mỗi lượt hỏi sinh
        # ra 2 dòng (user + assistant) dùng chung request_id.
        Index("uq_chat_messages_request_role", "request_id", "role", unique=True),
        Index("idx_chat_messages_session_id", "session_id", "id"),
        # Danh sách hội thoại của app (GET /v1/conversations) gom theo user.
        Index("idx_chat_messages_user_id", "user_id", "id"),
    )


DOCUMENT_STATUSES = ("received", "processing", "done", "skipped", "unreadable", "quota_exceeded", "failed")


class HealthDocument(Base):
    """Ảnh giấy tờ y tế user gửi qua chat (docs/superpowers/specs/
    2026-11-06-chat-image-ocr-design.md). Ảnh gốc nằm trên Supabase Storage,
    ở đây chỉ giữ đường dẫn."""

    __tablename__ = "health_documents"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Hội thoại gần nhất user gửi ảnh này: ngữ cảnh chat lấy tài liệu theo cột này.
    conversation_id: Mapped[UUID | None] = mapped_column(Uuid)
    # Chuỗi app tự định nghĩa (thành viên gia đình); None = chủ tài khoản.
    profile_id: Mapped[str | None] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    doc_type: Mapped[str | None] = mapped_column(String(20))
    confidence: Mapped[float | None] = mapped_column(Float)
    title: Mapped[str | None] = mapped_column(Text)
    doc_date: Mapped[date | None] = mapped_column(Date)
    # model, prompt version, token, chi phí, lý do failed
    meta: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)
    # Gán ở Python (không server_default): cursor phân trang so theo cột này,
    # cần độ chính xác micro giây cả trên SQLite.
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('received', 'processing', 'done', 'skipped', 'unreadable', 'quota_exceeded', 'failed')",
            name="ck_health_documents_status",
        ),
        Index("idx_health_documents_user_created", "user_id", created_at.desc()),
        Index("idx_health_documents_conversation", "conversation_id"),
        # Cùng user gửi lại đúng ảnh cũ thì dùng lại bản ghi, không đọc lại.
        Index(
            "uq_health_documents_user_sha256", "user_id", "sha256", unique=True,
            postgresql_where=text("deleted_at IS NULL"), sqlite_where=text("deleted_at IS NULL"),
        ),
    )


class HealthDocumentOcr(Base):
    """Chữ chép nguyên văn từ ảnh. Tách bảng: dài, và chỉ màn chi tiết cần."""

    __tablename__ = "health_document_ocr"

    document_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("health_documents.id", ondelete="CASCADE"), primary_key=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HealthDocumentExtraction(Base):
    """Dữ liệu có cấu trúc trích từ ảnh. Nhiều phiên bản (đọc lại bằng model
    tốt hơn vẫn giữ lịch sử); bản id lớn nhất là bản dùng."""

    __tablename__ = "health_document_extractions"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    document_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("health_documents.id", ondelete="CASCADE"), nullable=False
    )
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)
    data: Mapped[dict] = mapped_column(JsonType, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("idx_health_document_extractions_document", "document_id", "id"),)
