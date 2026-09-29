import logging
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.config import settings
from app.db.repository import ConversationContext
from app.prompts import clip, fence, load_prompt, load_system_prompt
from app.schemas import ChatAnswer, SessionSummary
from app.services.cost import Usage, from_completion
from app.services.retry import call_with_backoff

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Generation:
    """Câu trả lời kèm token đã dùng — usage là đầu vào để đo chi phí thật."""

    answer: ChatAnswer
    usage: Usage


class TruncatedCompletion(Exception):
    """
    Model bị cắt giữa chừng vì chạm trần `max_tokens`.

    Tách thành lỗi riêng thay vì để pydantic ném ValidationError khi parse
    JSON dở: hai nguyên nhân này cần hai cách xử lý khác hẳn nhau. JSON sai
    schema là model trả sai; cắt cụt là TRẦN của mình đặt thấp quá, và retry
    nguyên xi thì lần nào cũng cắt đúng chỗ đó. Ghi rõ ra DLQ để còn biết mà
    nâng `openai_max_output_tokens`.
    """


_client: AsyncOpenAI | None = None


def start_openai() -> None:
    global _client
    # max_retries=0: SDK mặc định tự retry 2 lần, chồng lên `call_with_backoff`
    # (app/services/retry.py) thành tới 9 lần thử × timeout cho một câu — vượt
    # xa SHUTDOWN_GRACE_SECONDS và chặn cả partition. Chỉ một nơi quyết định
    # retry.
    _client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout_seconds,
        max_retries=0,
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


def _as_question(content: str, max_chars: int | None = None) -> str:
    return fence("user_question", content, max_chars)


def _parse(completion, model):
    """Đọc nội dung completion ra schema, phân biệt rõ 'bị cắt' với 'trả sai'."""
    choice = completion.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise TruncatedCompletion(
            f"Model chạm trần {settings.openai_max_output_tokens} token đầu ra, "
            f"JSON trả về không hoàn chỉnh"
        )
    return model.model_validate_json(choice.message.content or "")


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

    Mọi khối dữ liệu đều dựng qua `fence()` chứ không nối chuỗi tay: nối tay
    thì người dùng chỉ cần gõ `</user_question>` là phần viết sau đó nằm NGOÀI
    khối, tức là đúng chỗ model đọc như chỉ dẫn. Ranh giới chỉ dẫn/dữ liệu chỉ
    có giá trị khi dữ liệu không tự đóng được khối của nó.
    """
    messages: list[dict] = [
        {"role": "system", "content": load_system_prompt(prompt_version)}
    ]

    if context is not None:
        if context.summary:
            messages.append(
                {
                    "role": "user",
                    "content": fence(
                        "session_summary",
                        context.summary,
                        settings.summary_max_chars,
                    ),
                }
            )
        limit = settings.history_message_max_chars
        for past in context.recent:
            if not past.content or not past.content.strip():
                # Lượt rỗng không thêm ngữ cảnh nào, chỉ tốn token và làm model
                # phải đoán xem khối trống đó nghĩa là gì.
                continue
            if past.role == "user":
                messages.append(
                    {"role": "user", "content": _as_question(past.content, limit)}
                )
            else:
                messages.append(
                    {"role": "assistant", "content": clip(past.content, limit)}
                )

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
    return Generation(
        answer=_parse(completion, ChatAnswer), usage=from_completion(completion)
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
                {
                    "role": "user",
                    # `transcript` do `summarizer.render_transcript` dựng từ các
                    # khối fence() đã gỡ thẻ và đã chặn độ dài theo từng khối.
                    # Gỡ thẻ lần nữa ở đây là xoá mất <user>/<assistant>, model
                    # tóm tắt không còn biết câu nào của ai.
                    "content": fence("transcript", transcript, sanitize=False),
                },
            ],
            response_format=_response_format("session_summary", SessionSummary),
            **_sampling_params(settings.openai_model),
        )
    )
    return _parse(completion, SessionSummary)
