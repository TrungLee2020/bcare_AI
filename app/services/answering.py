"""
Ghép 3 lớp của Phase 3 lại: lọc input -> gọi OpenAI -> validate output.

Tách khỏi consumer để test được toàn bộ logic trả lời mà không cần Kafka.
"""

import logging

from app.config import settings
from app.db.repository import ConversationContext
from app.schemas import ChatAnswer, ChatRequestMessage, ChatResponseMessage
from app import metrics
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


async def answer_question(
    message: ChatRequestMessage,
    context: ConversationContext | None = None,
    model: str | None = None,
) -> ChatResponseMessage:
    """
    Sinh câu trả lời cho 1 request, có thể kèm ngữ cảnh hội thoại (Phase 4). Lỗi gọi OpenAI được ném lên cho consumer
    (retry/backoff/dead-letter là việc của Phase 5).
    """
    version = settings.prompt_version
    model = model or settings.openai_model

    def _response(
        answer: ChatAnswer, status: str, detail: str = "", usage: dict | None = None
    ) -> ChatResponseMessage:
        metrics.incr(f"answers_{status}")
        return ChatResponseMessage(
            request_id=message.request_id,
            user_id=message.user_id,
            session_id=message.session_id,
            status=status,
            answer=answer,
            prompt_version=version,
            model=model,
            detail=detail,
            usage=usage or {},
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
        metrics.incr("input_blocked")
        # Câu bị chặn mà có dấu hiệu cấp cứu thì vẫn phải bảo người ta đi cấp
        # cứu: chặn nhầm một câu thật là chuyện có thể xảy ra (xem
        # input_filter), và cái giá của việc im lặng ở đây quá đắt.
        blocked = (
            output_validator.EMERGENCY_FALLBACK_ANSWER
            if output_validator.mentions_emergency(message.content)
            else BLOCKED_ANSWER
        )
        return _response(blocked, "blocked", ",".join(verdict.reasons))

    health_context = message.health_context
    if health_context and input_filter.check(health_context).blocked:
        # Hồ sơ cũng là chữ người dùng tự gõ. Có dấu hiệu injection thì bỏ hồ
        # sơ, vẫn trả lời câu hỏi — không chặn cả lượt vì một ô ghi chú.
        logger.warning("Bỏ health_context có dấu hiệu injection request_id=%s", message.request_id)
        metrics.incr("health_context_dropped")
        health_context = None

    generation = await openai_client.generate(
        message.content, version, context, model, health_context
    )
    usage = generation.usage.as_dict(model)
    metrics.incr("openai_calls")
    metrics.incr(f"openai_calls:{model}")
    metrics.incr("tokens_total", generation.usage.total_tokens)
    metrics.incr("cost_usd", usage["cost_usd"])

    outcome = output_validator.validate(generation.answer, version, message.content)
    if not outcome.ok:
        logger.warning(
            "Câu trả lời không qua validate request_id=%s reasons=%s",
            message.request_id,
            outcome.reasons,
        )
        metrics.incr("output_blocked")
        return _response(outcome.answer, "blocked", outcome.detail, usage)

    return _response(outcome.answer, "ok", usage=usage)


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


PROCESSING_ANSWER = ChatAnswer(
    answer="Mình đang tìm câu trả lời cho bạn, chờ một chút nhé...",
    out_of_scope=False,
    refusal_reason="",
    should_see_doctor=False,
    follow_up_questions=[],
)
