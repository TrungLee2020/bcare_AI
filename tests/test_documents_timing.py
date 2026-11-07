"""
Bất biến thời gian của một tin có ảnh: đọc ảnh (ngân sách của tin) rồi trả lời
(timeout OpenAI × số lần thử + backoff) phải xong trước khi consumer hết thời
gian chờ lúc tắt máy, và trước khi /v1/chat bỏ chờ câu trả lời.

Kiểm trên giá trị mặc định trong code (không phải `.env` của máy chạy test).
"""

from app.config import Settings
from app.kafka import consumer


def default(name: str):
    return Settings.model_fields[name].default


def answer_retry_budget() -> float:
    attempts = default("openai_max_attempts")
    backoff_cap = sum(
        min(default("openai_retry_max_delay"), default("openai_retry_base_delay") * 2 ** (attempt - 1))
        for attempt in range(1, attempts)
    )
    return default("openai_timeout_seconds") * attempts + backoff_cap


def test_one_model_call_fits_in_the_message_budget():
    assert default("ocr_llm_budget_seconds") <= default("ocr_message_budget_seconds")


def test_image_message_finishes_within_shutdown_grace():
    worst = default("ocr_message_budget_seconds") + answer_retry_budget()
    assert worst <= consumer.SHUTDOWN_GRACE_SECONDS
    assert worst <= default("v1_answer_timeout_seconds")


def test_rebalance_timeout_exceeds_shutdown_grace():
    assert consumer.REBALANCE_TIMEOUT_MS > consumer.SHUTDOWN_GRACE_SECONDS * 1000
