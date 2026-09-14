"""
Nén lịch sử cũ của 1 phiên thành `chat_sessions.summary`.

Vì sao cần: nhồi toàn bộ lịch sử vào prompt vừa đắt (trả tiền theo token, mỗi
câu hỏi lại chở lại toàn bộ quá khứ) vừa làm chất lượng trả lời kém đi (prompt
dài thì model loãng). Tóm tắt cho phép giữ bối cảnh dài hạn với chi phí gần như
không đổi theo độ dài phiên.
"""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import repository
from app.db.models import ChatMessage
from app.prompts import leaks_system_prompt
from app.services import openai_client

logger = logging.getLogger(__name__)


def render_transcript(messages: list[ChatMessage]) -> str:
    labels = {"user": "Người dùng", "assistant": "Trợ lý"}
    return "\n".join(f"{labels.get(m.role, m.role)}: {m.content}" for m in messages)


def _is_safe(summary: str) -> bool:
    """
    Summary là văn bản do model sinh ra rồi được nạp LẠI vào prompt của mọi câu
    hỏi sau trong phiên. Một summary bị nhiễm sẽ đầu độc toàn bộ phần còn lại
    của cuộc hội thoại, nên phải soi trước khi ghi xuống DB.
    """
    if not summary.strip():
        return False
    if len(summary) > settings.summary_max_chars:
        return False
    return not leaks_system_prompt(summary, settings.prompt_version)


async def maybe_summarize(db: AsyncSession, session_id: UUID) -> bool:
    """
    Tóm tắt nếu số message chưa nén đã vượt ngưỡng. Trả về True nếu có cập nhật.

    Chạy ngay trong consumer (sau mỗi K message) thay vì cron job riêng: consumer
    đã xử lý tuần tự theo từng partition/user nên không có 2 tiến trình cùng tóm
    tắt 1 phiên, khỏi cần khoá phân tán.
    """
    pending = await repository.count_unsummarized(db, session_id)
    if pending < settings.summary_trigger_messages:
        return False

    messages, through_id = await repository.load_messages_for_summary(
        db, session_id, keep_recent=settings.history_window_messages
    )
    if not messages or through_id is None:
        return False

    try:
        result = await openai_client.summarize(render_transcript(messages))
    except Exception:
        # Tóm tắt lỗi không được làm hỏng câu trả lời đã sinh xong. Bỏ qua, lần
        # sau ngưỡng vẫn vượt nên sẽ thử lại.
        logger.exception("Tóm tắt phiên %s thất bại, bỏ qua lần này", session_id)
        return False

    if not _is_safe(result.summary):
        logger.warning(
            "Summary của phiên %s không qua kiểm tra an toàn, không ghi xuống DB",
            session_id,
        )
        return False

    await repository.update_summary(db, session_id, result.summary, through_id)
    logger.info(
        "Đã tóm tắt phiên %s (%d message, mốc id=%s)",
        session_id,
        len(messages),
        through_id,
    )
    return True
