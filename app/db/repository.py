"""Truy vấn lịch sử chat. Mọi hàm nhận sẵn AsyncSession để caller quyết định
ranh giới transaction."""

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatMessage, ChatSession

__all__ = ["ChatMessage", "ChatSession", "ConversationContext"]


@dataclass
class ConversationContext:
    """Ngữ cảnh nạp cho 1 câu hỏi mới."""

    summary: str | None = None
    # Các lượt gần nhất, theo thứ tự CŨ -> MỚI (đúng thứ tự hội thoại)
    recent: list[ChatMessage] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.summary and not self.recent


async def ensure_session(db: AsyncSession, session_id: UUID, user_id: int) -> ChatSession:
    """Lấy phiên chat, tạo mới nếu chưa có."""
    existing = await db.get(ChatSession, session_id)
    if existing is not None:
        return existing
    session = ChatSession(id=session_id, user_id=user_id)
    db.add(session)
    await db.flush()
    return session


async def load_context(
    db: AsyncSession, session_id: UUID, limit: int
) -> ConversationContext:
    """
    Nạp summary + `limit` message gần nhất CHƯA nằm trong summary.

    Lọc theo `summarized_through_id` để không nhồi trùng: phần đã tóm tắt thì
    chở bằng summary, phần chưa tóm tắt mới chở nguyên văn.
    """
    session = await db.get(ChatSession, session_id)
    if session is None:
        return ConversationContext()

    query = select(ChatMessage).where(ChatMessage.session_id == session_id)
    if session.summarized_through_id is not None:
        query = query.where(ChatMessage.id > session.summarized_through_id)
    # Lấy N cái MỚI NHẤT rồi đảo lại, không phải N cái cũ nhất
    query = query.order_by(ChatMessage.id.desc()).limit(limit)

    rows = list((await db.scalars(query)).all())
    return ConversationContext(summary=session.summary, recent=list(reversed(rows)))


async def save_turn(
    db: AsyncSession,
    *,
    session_id: UUID,
    user_id: int,
    request_id: UUID,
    question: str,
    answer: str,
    meta: dict,
) -> None:
    """
    Ghi 1 lượt hỏi-đáp (2 dòng: user + assistant).

    ON CONFLICT DO NOTHING trên (request_id, role): nếu Redis mất dữ liệu và
    cùng 1 message bị xử lý lại, lịch sử vẫn không bị nhân đôi.
    """
    rows = [
        dict(
            session_id=session_id,
            user_id=user_id,
            request_id=request_id,
            role="user",
            content=question,
            meta={},
        ),
        dict(
            session_id=session_id,
            user_id=user_id,
            request_id=request_id,
            role="assistant",
            content=answer,
            meta=meta,
        ),
    ]
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        stmt = pg_insert(ChatMessage).values(rows)
        await db.execute(stmt.on_conflict_do_nothing(index_elements=["request_id", "role"]))
    else:
        # SQLite (test) dùng cú pháp khác; hành vi mong muốn là như nhau.
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(ChatMessage).values(rows)
        await db.execute(stmt.on_conflict_do_nothing(index_elements=["request_id", "role"]))


async def count_unsummarized(db: AsyncSession, session_id: UUID) -> int:
    session = await db.get(ChatSession, session_id)
    if session is None:
        return 0
    query = select(func.count()).select_from(ChatMessage).where(
        ChatMessage.session_id == session_id
    )
    if session.summarized_through_id is not None:
        query = query.where(ChatMessage.id > session.summarized_through_id)
    return int(await db.scalar(query) or 0)


async def load_messages_for_summary(
    db: AsyncSession, session_id: UUID, keep_recent: int
) -> tuple[list[ChatMessage], int | None]:
    """
    Lấy các message cần đưa vào summary: tất cả message chưa tóm tắt, TRỪ
    `keep_recent` cái mới nhất (những cái đó vẫn được chở nguyên văn trong
    prompt nên chưa cần nén).

    Trả về (messages, mốc id mới) — mốc là id lớn nhất trong nhóm được tóm tắt.
    """
    session = await db.get(ChatSession, session_id)
    if session is None:
        return [], None

    query = select(ChatMessage).where(ChatMessage.session_id == session_id)
    if session.summarized_through_id is not None:
        query = query.where(ChatMessage.id > session.summarized_through_id)
    rows = list((await db.scalars(query.order_by(ChatMessage.id))).all())

    to_summarize = rows[:-keep_recent] if keep_recent else rows
    if not to_summarize:
        return [], None
    return to_summarize, to_summarize[-1].id


async def update_summary(
    db: AsyncSession, session_id: UUID, summary: str, through_id: int
) -> None:
    session = await db.get(ChatSession, session_id)
    if session is None:
        return
    session.summary = summary
    session.summarized_through_id = through_id
    session.updated_at = func.now()
    await db.flush()
