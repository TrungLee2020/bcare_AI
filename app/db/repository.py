"""Truy vấn lịch sử chat. Mọi hàm nhận sẵn AsyncSession để caller quyết định
ranh giới transaction."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
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


async def ensure_session(db: AsyncSession, session_id: UUID, user_id: str) -> ChatSession:
    """Lấy phiên chat, tạo mới nếu chưa có."""
    existing = await db.get(ChatSession, session_id)
    if existing is not None:
        return existing
    session = ChatSession(id=session_id, user_id=user_id)
    db.add(session)
    await db.flush()
    return session


async def load_context(
    db: AsyncSession, session_id: UUID, user_id: str, limit: int
) -> ConversationContext:
    """
    Nạp summary + `limit` message gần nhất CHƯA nằm trong summary.

    Lọc theo `summarized_through_id` để không nhồi trùng: phần đã tóm tắt thì
    chở bằng summary, phần chưa tóm tắt mới chở nguyên văn.

    Phiên của user KHÁC thì trả ngữ cảnh rỗng: `session_id` do client gửi lên,
    không lọc theo chủ phiên là ai có session_id của người khác cũng đọc được
    lịch sử sức khoẻ của họ (qua câu trả lời của model).
    """
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user_id or session.deleted_at is not None:
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
    user_id: str,
    request_id: UUID,
    question: str,
    answer: str,
    meta: dict,
    user_meta: dict | None = None,
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
            meta=user_meta or {},
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


@dataclass
class ConversationOverview:
    """Một dòng trong danh sách hội thoại của app."""

    session_id: UUID
    first: ChatMessage
    last: ChatMessage
    message_count: int


async def list_conversations(
    db: AsyncSession, user_id: str, limit: int, before_id: int | None = None
) -> list[ConversationOverview]:
    """
    Hội thoại của user, mới hoạt động nhất trước. Sắp và phân trang theo id
    message cuối của từng hội thoại: id tăng dần theo thời gian ghi, nên không
    cần cột updated_at phải được cập nhật đúng ở mọi chỗ ghi.

    `before_id`: chỉ lấy hội thoại có message cuối < before_id (trang sau).
    Hội thoại user đã xoá không hiện.
    """
    last_id = func.max(ChatMessage.id).label("last_id")
    query = (
        select(ChatMessage.session_id, func.min(ChatMessage.id), last_id, func.count())
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(ChatMessage.user_id == user_id, ChatSession.deleted_at.is_(None))
        .group_by(ChatMessage.session_id)
        .order_by(last_id.desc())
        .limit(limit)
    )
    if before_id is not None:
        query = query.having(func.max(ChatMessage.id) < before_id)
    groups = (await db.execute(query)).all()
    if not groups:
        return []

    ids = {g[1] for g in groups} | {g[2] for g in groups}
    rows = {m.id: m for m in (await db.scalars(select(ChatMessage).where(ChatMessage.id.in_(ids)))).all()}
    return [
        ConversationOverview(session_id=sid, first=rows[first_id], last=rows[last_id_], message_count=count)
        for sid, first_id, last_id_, count in groups
    ]


async def list_messages(
    db: AsyncSession, session_id: UUID, user_id: str, limit: int, before_id: int | None = None
) -> list[ChatMessage] | None:
    """
    `limit` message mới nhất của phiên (có `before_id` thì chỉ lấy message cũ
    hơn), trả theo thứ tự CŨ -> MỚI. None khi không có phiên, phiên đã xoá
    hoặc phiên của user khác: cùng một kết quả để không lộ phiên nào tồn tại.
    """
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user_id or session.deleted_at is not None:
        return None
    query = select(ChatMessage).where(ChatMessage.session_id == session_id)
    if before_id is not None:
        query = query.where(ChatMessage.id < before_id)
    rows = list((await db.scalars(query.order_by(ChatMessage.id.desc()).limit(limit))).all())
    return list(reversed(rows))


async def soft_delete_conversation(db: AsyncSession, session_id: UUID, user_id: str) -> bool:
    """Ẩn một hội thoại. False khi không có, của user khác, hoặc đã xoá."""
    result = await db.execute(
        update(ChatSession)
        .where(ChatSession.id == session_id, ChatSession.user_id == user_id, ChatSession.deleted_at.is_(None))
        .values(deleted_at=func.now())
    )
    return result.rowcount > 0


async def soft_delete_all(db: AsyncSession, user_id: str) -> int:
    """Ẩn mọi hội thoại của user (app gọi trước khi xoá tài khoản)."""
    result = await db.execute(
        update(ChatSession)
        .where(ChatSession.user_id == user_id, ChatSession.deleted_at.is_(None))
        .values(deleted_at=func.now())
    )
    return result.rowcount


async def purge_deleted(db: AsyncSession, deleted_before: datetime) -> int:
    """
    Xoá hẳn các hội thoại đã bị ẩn trước `deleted_before`, kèm message và
    summary. Xoá message tường minh thay vì dựa vào ON DELETE CASCADE: SQLite
    (test) chỉ cascade khi bật PRAGMA foreign_keys trên đúng connection đó.
    """
    expired = select(ChatSession.id).where(ChatSession.deleted_at < deleted_before)
    await db.execute(delete(ChatMessage).where(ChatMessage.session_id.in_(expired)))
    result = await db.execute(delete(ChatSession).where(ChatSession.deleted_at < deleted_before))
    return result.rowcount


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
