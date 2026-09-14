"""
Retry với exponential backoff cho các lỗi TẠM THỜI khi gọi OpenAI.

Nguyên tắc: chỉ retry thứ có cơ hội thành công ở lần sau. Retry một lỗi vĩnh
viễn (sai API key, request không hợp lệ) chỉ làm chậm thêm rồi vẫn hỏng, mà
message thì bị giữ trong partition lâu hơn — cả các user khác trong partition
đó cùng phải chờ.
"""

import asyncio
import logging
import random

import openai

logger = logging.getLogger(__name__)

# Lỗi có thể hết sau vài giây: quá tải, nghẽn mạng, 5xx phía OpenAI.
RETRYABLE = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RETRYABLE)


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """
    Exponential backoff + jitter.

    Jitter là bắt buộc chứ không phải cho đẹp: khi OpenAI rate-limit, TẤT CẢ
    consumer đều dính cùng lúc: không có jitter thì chúng cùng ngủ đúng bằng
    nhau rồi cùng thức dậy đập vào API một lượt, và lại bị rate-limit tiếp.
    """
    return min(cap, base * (2 ** (attempt - 1))) * (0.5 + random.random() / 2)


async def call_with_backoff(factory, *, attempts: int, base_delay: float, max_delay: float):
    """
    Gọi `factory()` (hàm trả về coroutine) tối đa `attempts` lần.

    Nhận factory chứ không nhận coroutine vì coroutine đã await hỏng thì không
    await lại được — mỗi lần thử phải tạo một lời gọi mới.
    """
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await factory()
        except Exception as exc:
            last = exc
            if not is_retryable(exc) or attempt == attempts:
                raise
            delay = backoff_delay(attempt, base_delay, max_delay)
            logger.warning(
                "Gọi OpenAI lỗi (%s), thử lại lần %d/%d sau %.1fs",
                type(exc).__name__,
                attempt + 1,
                attempts,
                delay,
            )
            await asyncio.sleep(delay)
    raise last  # không tới được, chỉ để type checker yên tâm
