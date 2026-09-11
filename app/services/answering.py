"""
Ghép 3 lớp của Phase 3 lại: lọc input -> gọi OpenAI -> validate output.

Tách khỏi consumer để test được toàn bộ logic trả lời mà không cần Kafka.
"""

import logging

from app.config import settings
from app.schemas import ChatAnswer, ChatRequestMessage, ChatResponseMessage
from app.services import input_filter, openai_client, output_validator

logger = logging.getLogger(__name__)

BLOCKED_ANSWER = ChatAnswer(
    answer=(
        "Mình chỉ hỗ trợ các câu hỏi về sức khoẻ và quyền lợi bảo hiểm thôi nhé. "
        "Bạn có thể hỏi mình về triệu chứng, cách chăm sóc sức khoẻ tại nhà, "
        "hoặc các điều khoản trong hợp đồng bảo hiểm."
    ),
    out_of_scope=True,
    refusal_reason="injection",
    should_see_doctor=False,
    follow_up_questions=[],
)


async def answer_question(message: ChatRequestMessage) -> ChatResponseMessage:
    """
    Sinh câu trả lời cho 1 request. Lỗi gọi OpenAI được ném lên cho consumer
    (retry/backoff/dead-letter là việc của Phase 5).
    """
    version = settings.prompt_version

    def _response(answer: ChatAnswer, status: str, detail: str = "") -> ChatResponseMessage:
        return ChatResponseMessage(
            request_id=message.request_id,
            user_id=message.user_id,
            session_id=message.session_id,
            status=status,
            answer=answer,
            prompt_version=version,
            model=settings.openai_model,
            detail=detail,
        )

    verdict = input_filter.check(message.content)
    if verdict.blocked:
        # Chặn trước khi gọi API: vừa đỡ tốn tiền, vừa không cho người dùng
        # dùng chính model để dò xem filter nào đang chạy.
        # Quota VẪN bị trừ (đã trừ ở tầng API) — nếu hoàn lại thì việc thử
        # injection trở thành miễn phí và không giới hạn.
        logger.warning(
            "Chặn injection request_id=%s user_id=%s reasons=%s matches=%s",
            message.request_id,
            message.user_id,
            verdict.reasons,
            verdict.matches,
        )
        return _response(BLOCKED_ANSWER, "blocked", ",".join(verdict.reasons))

    raw_answer = await openai_client.generate(message.content, version)

    outcome = output_validator.validate(raw_answer, version)
    if not outcome.ok:
        logger.warning(
            "Câu trả lời không qua validate request_id=%s reasons=%s",
            message.request_id,
            outcome.reasons,
        )
        return _response(outcome.answer, "blocked", outcome.detail)

    return _response(outcome.answer, "ok")


ERROR_ANSWER = ChatAnswer(
    answer=(
        "Xin lỗi, hệ thống đang gặp sự cố nên chưa trả lời được câu hỏi của bạn. "
        "Lượt hỏi này không bị tính, bạn vui lòng thử lại sau ít phút nhé."
    ),
    out_of_scope=False,
    refusal_reason="",
    should_see_doctor=False,
    follow_up_questions=[],
)
