import logging

from openai import AsyncOpenAI

from app.config import settings
from app.prompts import load_system_prompt
from app.schemas import ChatAnswer

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


def _response_format() -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "chat_answer",
            # strict=True mới thật sự ép được schema; thiếu cờ này thì
            # json_schema chỉ là gợi ý và model vẫn có thể trả thiếu field.
            "strict": True,
            "schema": ChatAnswer.model_json_schema(),
        },
    }


def build_messages(content: str, prompt_version: str) -> list[dict]:
    """
    Câu hỏi của user được bọc trong <user_question> và đặt ở user turn riêng.

    Không bao giờ nối chuỗi câu hỏi vào system prompt: làm vậy là xoá ranh giới
    giữa "chỉ dẫn" và "dữ liệu", và mọi câu injection sẽ được model đọc với
    đúng thẩm quyền của system prompt.
    """
    return [
        {"role": "system", "content": load_system_prompt(prompt_version)},
        {"role": "user", "content": f"<user_question>\n{content}\n</user_question>"},
    ]


async def generate(content: str, prompt_version: str | None = None) -> ChatAnswer:
    """Gọi OpenAI và parse ra ChatAnswer. Lỗi mạng/API được ném lên cho caller
    xử lý (retry + dead-letter là việc của Phase 5)."""
    version = prompt_version or settings.prompt_version
    completion = await get_openai().chat.completions.create(
        model=settings.openai_model,
        messages=build_messages(content, version),
        response_format=_response_format(),
        temperature=settings.openai_temperature,
        max_tokens=settings.openai_max_output_tokens,
    )
    raw = completion.choices[0].message.content or ""
    return ChatAnswer.model_validate_json(raw)
