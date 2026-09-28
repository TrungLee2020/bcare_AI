import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.config import settings
from app.db.repository import ConversationContext
from app.prompts import load_prompt, load_system_prompt
from app.schemas import ChatAnswer, SessionSummary
from app.services.cost import Usage, from_completion
from app.services.retry import call_with_backoff

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Generation:
    """Câu trả lời kèm token đã dùng — usage là đầu vào để đo chi phí thật."""

    answer: ChatAnswer
    usage: Usage

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


def _is_reasoning_model(model: str) -> bool:
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def _sampling_params(model: str) -> dict:
    """
    Model reasoning (gpt-5*, o*) không nhận `temperature` khác mặc định và bỏ
    hẳn `max_tokens`. `max_completion_tokens` của chúng tính cả token suy luận,
    nên cộng thêm ngân sách suy luận để không bị hết giữa chừng rồi trả về rỗng.
    """
    params = {"max_completion_tokens": settings.openai_max_output_tokens}
    if _is_reasoning_model(model):
        params["max_completion_tokens"] += settings.openai_reasoning_token_budget
        params["reasoning_effort"] = settings.openai_reasoning_effort
    else:
        params["temperature"] = settings.openai_temperature
    return params


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
    model: str | None = None,
) -> Generation:
    """Gọi OpenAI và parse ra ChatAnswer kèm token usage. Lỗi mạng/API được ném
    lên cho caller xử lý (retry ở tầng dưới, dead-letter ở consumer)."""
    version = prompt_version or settings.prompt_version
    model = model or settings.openai_model
    completion = await _with_retry(
        lambda: get_openai().chat.completions.create(
            model=model,
            messages=build_messages(content, version, context),
            response_format=_response_format(),
            **_sampling_params(model),
        )
    )
    raw = completion.choices[0].message.content or ""
    return Generation(
        answer=ChatAnswer.model_validate_json(raw), usage=from_completion(completion)
    )


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
            **_sampling_params(settings.openai_model),
        )
    )
    return SessionSummary.model_validate_json(completion.choices[0].message.content or "")
