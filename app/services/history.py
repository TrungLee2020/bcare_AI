"""Cầu nối giữa consumer và tầng DB: nạp ngữ cảnh, ghi lại lượt hỏi-đáp."""

import logging

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
            db, message.session_id, settings.history_window_messages
        )


async def record_turn(
    message: ChatRequestMessage, response: ChatResponseMessage
) -> None:
    """Ghi lượt hỏi-đáp vào lịch sử rồi tóm tắt nếu tới ngưỡng."""
    if message.session_id is None:
        return

    async with db_session() as db:
        await repository.ensure_session(db, message.session_id, message.user_id)
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
                "detail": response.detail,
            },
        )
        await summarizer.maybe_summarize(db, message.session_id)
