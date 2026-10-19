"""
Ước tính chi phí OpenAI theo token đã dùng.

Plan Phase 6 yêu cầu "theo dõi chi phí OpenAI thực tế so với ước tính đã review
trước đó" — muốn so thì phải đo, nên mỗi lần gọi API đều ghi lại token usage.
"""

from dataclasses import dataclass

from app import metrics
from app.config import settings


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int = 0
    # Đã GỒM token suy luận ẩn của model reasoning (OpenAI tính tiền như
    # token đầu ra), nên chi phí tính từ trường này là đủ.
    completion_tokens: int = 0
    # Phần suy luận ẩn trong completion_tokens: người dùng không thấy, chỉ để
    # biết OPENAI_REASONING_EFFORT / OPENAI_REASONING_TOKEN_BUDGET có hợp lý.
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def as_dict(self, model: str | None = None) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": round(estimate_cost(self, model), 6),
        }


def from_completion(completion) -> Usage:
    """Đọc usage từ response của OpenAI. Thiếu trường usage thì trả về 0 chứ
    không nổ — thiếu số liệu chi phí không đáng để hỏng cả câu trả lời."""
    usage = getattr(completion, "usage", None)
    if usage is None:
        return Usage()
    details = getattr(usage, "completion_tokens_details", None)
    return Usage(
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0,
    )


def record(usage: Usage, model: str | None) -> dict:
    """Cộng usage của 1 lần gọi vào /metrics, tổng và theo từng model. Trả
    về `as_dict` để nơi gọi ghi log nếu cần."""
    data = usage.as_dict(model)
    name = model or "unknown"
    metrics.incr("openai_calls")
    metrics.incr(f"openai_calls:{name}")
    metrics.incr("tokens_total", usage.total_tokens)
    metrics.incr("cost_usd", data["cost_usd"])
    metrics.incr(f"cost_usd:{name}", data["cost_usd"])
    metrics.incr(f"completion_tokens:{name}", usage.completion_tokens)
    metrics.incr(f"reasoning_tokens:{name}", usage.reasoning_tokens)
    return data


def model_prices() -> dict[str, tuple[float, float]]:
    """Đọc MODEL_PRICES dạng "model=in,out;model2=in,out"."""
    prices = {}
    for item in settings.model_prices.split(";"):
        if not item.strip():
            continue
        name, _, pair = item.partition("=")
        inp, out = pair.split(",")
        prices[name.strip()] = (float(inp), float(out))
    return prices


def price_for(model: str | None) -> tuple[float, float]:
    return model_prices().get(
        model or "", (settings.price_input_per_1m, settings.price_output_per_1m)
    )


def estimate_cost(usage: Usage, model: str | None = None) -> float:
    """USD cho 1 lần gọi, theo đơn giá của đúng model đã gọi. Đơn giá lấy từ
    config — xem cảnh báo ở app/config.py về việc phải đối chiếu lại với bảng
    giá hiện hành."""
    price_in, price_out = price_for(model)
    return (usage.prompt_tokens * price_in + usage.completion_tokens * price_out) / 1_000_000
