import logging

from openai import AsyncOpenAI

from app.config import settings
from app.db.repository import ConversationContext
from app.prompts import load_prompt, load_system_prompt
from app.schemas import ChatAnswer, SessionSummary
from app.services.retry import call_with_backoff

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def start_openai() -> None:
    global _client
    _client = AsyncOpenAI(
        api_key=settings.openai_api_key, timeout=settings.openai_timeout_seconds
    )
    logger.info("OpenAI client sẵn sàng (model=%s)", settings.openai_model)


async def stop_openai() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


def get_openai() -> AsyncOpenAI:
    if _client is None:
        raise RuntimeError("OpenAI client chưa được start (gọi start_openai() trước)")
    return _client


def set_openai(client) -> None:
    """Dùng cho test: inject client giả, không gọi API thật."""
    global _client
    _client = client


async def _with_retry(factory):
    return await call_with_backoff(
        factory,
        attempts=settings.openai_max_attempts,
        base_delay=settings.openai_retry_base_delay,
        max_delay=settings.openai_retry_max_delay,
    )


def _response_format(name: str = "chat_answer", model=None) -> dict:
    model = model or ChatAnswer
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            # strict=True mới thật sự ép được schema; thiếu cờ này thì
            # json_schema chỉ là gợi ý và model vẫn có thể trả thiếu field.
            "strict": True,
            "schema": model.model_json_schema(),
        },
    }


def _as_question(content: str) -> str:
    return f"<user_question>\n{content}\n</user_question>"


def build_messages(
    content: str, prompt_version: str, context: ConversationContext | None = None
) -> list[dict]:
    """
    Câu hỏi của user được bọc trong <user_question> và đặt ở user turn riêng.

    Không bao giờ nối chuỗi câu hỏi vào system prompt: làm vậy là xoá ranh giới
    giữa "chỉ dẫn" và "dữ liệu", và mọi câu injection sẽ được model đọc với
    đúng thẩm quyền của system prompt.

    Ngữ cảnh (summary + lịch sử) cũng theo đúng nguyên tắc đó. Summary đặc biệt
    nguy hiểm vì nó là văn bản do MODEL sinh ra rồi được nạp lại vào prompt —
    nếu nhét vào system turn thì một câu injection trong lịch sử có thể được
    "rửa" qua bước tóm tắt để leo lên thành chỉ dẫn hệ thống. Vì vậy summary
    luôn đi ở user turn, trong khối <session_summary>.
    """
    messages: list[dict] = [
        {"role": "system", "content": load_system_prompt(prompt_version)}
    ]

    if context is not None:
        if context.summary:
            messages.append(
                {
                    "role": "user",
                    "content": f"<session_summary>\n{context.summary}\n</session_summary>",
                }
            )
        for past in context.recent:
            if past.role == "user":
                messages.append({"role": "user", "content": _as_question(past.content)})
            else:
                messages.append({"role": "assistant", "content": past.content})

    messages.append({"role": "user", "content": _as_question(content)})
    return messages


async def generate(
    content: str,
    prompt_version: str | None = None,
    context: ConversationContext | None = None,
) -> ChatAnswer:
    """Gọi OpenAI và parse ra ChatAnswer. Lỗi mạng/API được ném lên cho caller
    xử lý (retry + dead-letter là việc của Phase 5)."""
    version = prompt_version or settings.prompt_version
    completion = await _with_retry(
        lambda: get_openai().chat.completions.create(
            model=settings.openai_model,
            messages=build_messages(content, version, context),
            response_format=_response_format(),
            temperature=settings.openai_temperature,
            max_tokens=settings.openai_max_output_tokens,
        )
    )
    raw = completion.choices[0].message.content or ""
    return ChatAnswer.model_validate_json(raw)


async def summarize(transcript: str, prompt_version: str | None = None) -> SessionSummary:
    """Nén các lượt cũ thành summary. Dùng prompt riêng (prompts/summary_*.md),
    không dùng chung system prompt trả lời."""
    version = prompt_version or settings.summary_prompt_version
    completion = await _with_retry(
        lambda: get_openai().chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": load_prompt("summary", version)},
                {"role": "user", "content": f"<transcript>\n{transcript}\n</transcript>"},
            ],
            response_format=_response_format("session_summary", SessionSummary),
            temperature=settings.openai_temperature,
            max_tokens=settings.openai_max_output_tokens,
        )
    )
    return SessionSummary.model_validate_json(completion.choices[0].message.content or "")
