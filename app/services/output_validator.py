"""
Lớp validate câu trả lời TRƯỚC khi trả về cho user.

Structured Outputs chỉ đảm bảo đúng *hình dạng* JSON, không đảm bảo đúng *nội
dung*. Lớp này bắt các trường hợp model đã bị dẫn dắt hoặc trôi khỏi phạm vi:
lộ system prompt, kê liều thuốc, chẩn đoán xác định, trả lời dài bất thường,
hoặc tự mâu thuẫn giữa out_of_scope và refusal_reason.
"""

import re
from dataclasses import dataclass, field

from app.config import settings
from app.prompts import leaks_system_prompt
from app.schemas import ChatAnswer
from app.services.input_filter import normalize

MAX_FOLLOW_UPS = 3

FALLBACK_ANSWER = ChatAnswer(
    answer=(
        "Xin lỗi, mình chưa đưa ra được câu trả lời phù hợp cho câu hỏi này. "
        "Bạn thử hỏi lại rõ hơn giúp mình nhé, hoặc liên hệ tổng đài bCare để "
        "được hỗ trợ trực tiếp. Nếu bạn đang có triệu chứng khiến bạn lo lắng, "
        "hãy đi khám để được bác sĩ đánh giá trực tiếp."
    ),
    out_of_scope=True,
    refusal_reason="off_topic",
    should_see_doctor=False,
    follow_up_questions=[],
)

# Kê đơn: tên thuốc kèm liều lượng. Bắt theo đơn vị thuốc để không dính nhầm
# các con số của bảo hiểm ("chi trả 500.000đ/ngày").
_PRESCRIPTION_PATTERNS = [
    r"\b\d+([.,]\d+)?\s*(mg|mcg|ml|iu)\b",
    r"\buong\b.{0,25}\b\d+\s*(vien|goi|ong)\b",
    r"\b\d+\s*(vien|goi)\b.{0,15}\b\d+\s*lan\s*/?\s*(mot )?ngay\b",
]

# Chẩn đoán xác định. Cố ý viết hẹp: "có thể bạn bị...", "khả năng là..." là
# hợp lệ, chỉ chặn khi model khẳng định chắc chắn.
_DIAGNOSIS_PATTERNS = [
    r"\b(chac chan|chan doan la|ket luan la|dung la)\b.{0,15}\bban (bi|mac)\b",
    r"\btoi chan doan\b",
    r"\bban da mac benh\b",
]

_COMPILED_PRESCRIPTION = [re.compile(p) for p in _PRESCRIPTION_PATTERNS]
_COMPILED_DIAGNOSIS = [re.compile(p) for p in _DIAGNOSIS_PATTERNS]


@dataclass
class ValidationOutcome:
    ok: bool
    answer: ChatAnswer
    reasons: list[str] = field(default_factory=list)

    @property
    def detail(self) -> str:
        return ",".join(self.reasons)


def validate(answer: ChatAnswer, prompt_version: str | None = None) -> ValidationOutcome:
    version = prompt_version or settings.prompt_version
    reasons: list[str] = []
    text = answer.answer.strip()
    normalized = normalize(text)

    if not text:
        reasons.append("empty_answer")
    if len(text) > settings.answer_max_chars:
        reasons.append("answer_too_long")
    if leaks_system_prompt(text, version):
        reasons.append("system_prompt_leak")
    if answer.out_of_scope and not answer.refusal_reason:
        reasons.append("missing_refusal_reason")
    if not answer.out_of_scope and answer.refusal_reason:
        reasons.append("inconsistent_refusal_reason")
    if any(p.search(normalized) for p in _COMPILED_PRESCRIPTION):
        reasons.append("prescription_in_answer")
    if any(p.search(normalized) for p in _COMPILED_DIAGNOSIS):
        reasons.append("definitive_diagnosis")

    if reasons:
        return ValidationOutcome(ok=False, answer=FALLBACK_ANSWER, reasons=reasons)

    # Không fail vì mấy lỗi nhỏ này, chỉ cắt gọn lại cho đúng hợp đồng với FE.
    cleaned = answer.model_copy(
        update={
            "answer": text,
            "follow_up_questions": [
                q.strip() for q in answer.follow_up_questions if q.strip()
            ][:MAX_FOLLOW_UPS],
        }
    )
    return ValidationOutcome(ok=True, answer=cleaned)
