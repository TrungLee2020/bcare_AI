"""
Ước tính chi phí OpenAI theo token đã dùng.

Plan Phase 6 yêu cầu "theo dõi chi phí OpenAI thực tế so với ước tính đã review
trước đó" — muốn so thì phải đo, nên mỗi lần gọi API đều ghi lại token usage.
"""

from dataclasses import dataclass

from app.config import settings


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": round(estimate_cost(self), 6),
        }


def from_completion(completion) -> Usage:
    """Đọc usage từ response của OpenAI. Thiếu trường usage thì trả về 0 chứ
    không nổ — thiếu số liệu chi phí không đáng để hỏng cả câu trả lời."""
    usage = getattr(completion, "usage", None)
    if usage is None:
        return Usage()
    return Usage(
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )


def estimate_cost(usage: Usage) -> float:
    """USD cho 1 lần gọi. Đơn giá lấy từ config — xem cảnh báo ở app/config.py
    về việc phải đối chiếu lại với bảng giá hiện hành."""
    return (
        usage.prompt_tokens * settings.price_input_per_1m
        + usage.completion_tokens * settings.price_output_per_1m
    ) / 1_000_000
