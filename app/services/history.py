"""Cầu nối giữa consumer và tầng DB: nạp ngữ cảnh, ghi lại lượt hỏi-đáp, xoá
hẳn các hội thoại user đã xoá."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.db import repository
from app.db.repository import ConversationContext
from app.db.session import db_session
from app.schemas import ChatRequestMessage, ChatResponseMessage
from app.services import summarizer

logger = logging.getLogger(__name__)


async def load_context(message: ChatRequestMessage) -> ConversationContext:
    """
    Nạp summary + N lượt gần nhất của phiên.

    Không có `session_id` thì không có ngữ cảnh: mỗi câu hỏi đứng một mình. FE
    cần gửi kèm session_id nếu muốn hội thoại nhớ được mạch trước đó.
    """
    if message.session_id is None:
        return ConversationContext()
    async with db_session() as db:
        return await repository.load_context(
            db, message.session_id, message.user_id, settings.history_window_messages
        )


async def record_turn(
    message: ChatRequestMessage, response: ChatResponseMessage
) -> None:
    """Ghi lượt hỏi-đáp vào lịch sử rồi tóm tắt nếu tới ngưỡng."""
    if message.session_id is None:
        return

    async with db_session() as db:
        session = await repository.ensure_session(db, message.session_id, message.user_id)
        if session.user_id != message.user_id:
            # Không ghi lượt của user này vào phiên của người khác: nếu ghi,
            # chủ phiên sẽ thấy nội dung lạ trong ngữ cảnh (và trong summary)
            # của chính họ — một đường chèn injection vào phiên người khác.
            logger.warning(
                "user_id=%s gửi session_id=%s thuộc user khác, không ghi lịch sử",
                message.user_id,
                message.session_id,
            )
            return
        if session.deleted_at is not None:
            # User xoá hội thoại trong lúc câu trả lời đang sinh: không ghi lại
            # thứ họ vừa xoá.
            logger.info("session_id=%s đã bị xoá, không ghi lịch sử", message.session_id)
            return
        await repository.save_turn(
            db,
            session_id=message.session_id,
            user_id=message.user_id,
            request_id=message.request_id,
            question=message.content,
            answer=response.answer.answer,
            meta={
                "status": response.status,
                "model": response.model,
                "prompt_version": response.prompt_version,
                "out_of_scope": response.answer.out_of_scope,
                "refusal_reason": response.answer.refusal_reason,
                "should_see_doctor": response.answer.should_see_doctor,
                # App mở lại hội thoại thì hiện lại gợi ý của lượt cuối.
                "follow_up_questions": response.answer.follow_up_questions,
                "detail": response.detail,
            },
        )

    # Transaction riêng: lượt hỏi-đáp phải commit ngay để app đọc lịch sử thấy
    # luôn, không chờ lần gọi OpenAI để tóm tắt (vài giây), và không giữ
    # connection DB trong lúc chờ OpenAI. Tóm tắt lỗi thì lượt vẫn còn.
    async with db_session() as db:
        await summarizer.maybe_summarize(db, message.session_id)


_purge_task: asyncio.Task | None = None


async def purge_deleted() -> int:
    """Xoá hẳn hội thoại đã bị ẩn quá HISTORY_PURGE_AFTER_DAYS ngày."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.history_purge_after_days)
    async with db_session() as db:
        return await repository.purge_deleted(db, cutoff)


async def _purge_forever() -> None:
    # Chạy trên mọi instance: DELETE theo mốc thời gian, chạy trùng cũng vô hại.
    while True:
        try:
            purged = await purge_deleted()
            if purged:
                logger.info("Đã xoá hẳn %d hội thoại user đã xoá", purged)
        except Exception:
            logger.exception("Xoá hẳn hội thoại đã xoá thất bại, thử lại lần sau")
        await asyncio.sleep(settings.history_purge_interval_seconds)


def start_purge() -> None:
    global _purge_task
    _purge_task = asyncio.create_task(_purge_forever())


async def stop_purge() -> None:
    global _purge_task
    task, _purge_task = _purge_task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
