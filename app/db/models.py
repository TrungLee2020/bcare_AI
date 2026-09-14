"""
Model SQLAlchemy — nguồn sự thật DUY NHẤT cho schema.

`db/schema.sql` được SINH RA từ file này (`python -m scripts.dump_schema`), không
sửa tay. Hai nơi định nghĩa schema thì sớm muộn cũng lệch nhau.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
    func,
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
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
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

    __table_args__ = (
        Index("idx_chat_sessions_user_created", "user_id", created_at.desc()),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    session_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
    )
