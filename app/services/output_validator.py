"""
Lớp validate câu trả lời TRƯỚC khi trả về cho user.

Structured Outputs chỉ đảm bảo đúng *hình dạng* JSON, không đảm bảo đúng *nội
dung*. Lớp này bắt các trường hợp model đã bị dẫn dắt hoặc trôi khỏi phạm vi:
lộ system prompt, kê liều thuốc, chẩn đoán xác định, trả lời dài bất thường,
hoặc tự mâu thuẫn giữa out_of_scope và refusal_reason.
"""

import re
from dataclasses import dataclass, field

from app import metrics
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

# Thay cho câu trả lời bị chặn khi câu hỏi HOẶC câu trả lời có dấu hiệu cấp cứu.
# FALLBACK_ANSWER ("thử hỏi lại rõ hơn") ở đúng tình huống này là nguy hiểm:
# model đã nói "gọi 115 ngay" nhưng lỡ kèm một liều thuốc, bị chặn vì liều thuốc,
# và người đang đau ngực nhận về lời khuyên hỏi lại cho rõ.
EMERGENCY_FALLBACK_ANSWER = ChatAnswer(
    answer=(
        "Những gì bạn mô tả có thể là dấu hiệu cần cấp cứu. Hãy gọi 115 hoặc "
        "đến cơ sở y tế gần nhất NGAY, không tự chờ xem có đỡ không. Nếu có người "
        "bên cạnh, hãy nhờ họ hỗ trợ và đưa bạn đi. Thông tin này chỉ để tham "
        "khảo, không thay thế việc được bác sĩ khám trực tiếp."
    ),
    out_of_scope=False,
    refusal_reason="",
    should_see_doctor=True,
    follow_up_questions=[],
)


def fallback_for(*texts: str) -> ChatAnswer:
    """Câu thay thế khi phải chặn: có dấu hiệu cấp cứu thì luôn ưu tiên nói
    'đi cấp cứu' trước mọi thứ khác."""
    if any(mentions_emergency(t) for t in texts if t):
        metrics.incr("emergency_fallback")
        return EMERGENCY_FALLBACK_ANSWER
    return FALLBACK_ANSWER


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

# Dấu hiệu cấp cứu. System prompt yêu cầu gặp các dấu hiệu này thì đặt
# `should_see_doctor` = true, nhưng đó là hành vi của model — trước giờ không có
# gì kiểm chứng. Danh sách cố ý viết hẹp, bám đúng các dấu hiệu nêu trong prompt.
_EMERGENCY_PATTERNS = [
    r"\bdau nguc (du doi|du don|lan ra (tay|vai|ham))\b|\bdau that nguc\b",
    r"\bkho tho\b",
    r"\bco giat\b",
    r"\bmeo mieng\b|\byeu nua nguoi\b|\bliet nua nguoi\b",
    r"\bmoi tim( tai)?\b|\btim tai\b",
    r"\bmat y thuc\b|\bngat xiu\b|\bhon me\b",
    r"\bchay mau (khong cam|o at)\b",
    r"\btu hai\b|\btu tu\b",
    r"\b(di|den) (cap cuu|benh vien) ngay\b|\bgoi (115|cap cuu)\b",
]

_COMPILED_PRESCRIPTION = [re.compile(p) for p in _PRESCRIPTION_PATTERNS]
_COMPILED_DIAGNOSIS = [re.compile(p) for p in _DIAGNOSIS_PATTERNS]
_COMPILED_EMERGENCY = [re.compile(p) for p in _EMERGENCY_PATTERNS]


def mentions_emergency(text: str) -> bool:
    normalized = normalize(text)
    return any(p.search(normalized) for p in _COMPILED_EMERGENCY)


@dataclass
class ValidationOutcome:
    ok: bool
    answer: ChatAnswer
    reasons: list[str] = field(default_factory=list)

    @property
    def detail(self) -> str:
        return ",".join(self.reasons)


def validate(
    answer: ChatAnswer, prompt_version: str | None = None, question: str = ""
) -> ValidationOutcome:
    version = prompt_version or settings.prompt_version
    reasons: list[str] = []
    text = answer.answer.strip()
    normalized = normalize(text)

    follow_ups = [q.strip() for q in answer.follow_up_questions if q.strip()]
    if not text:
        reasons.append("empty_answer")
    if len(text) > settings.answer_max_chars:
        reasons.append("answer_too_long")
    # Soi cả follow-up: chúng cũng được hiển thị cho user, nên một câu hướng dẫn
    # bị chép vào đó là lộ prompt y như chép vào `answer`.
    if leaks_system_prompt("\n".join([text, *follow_ups]), version):
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
        return ValidationOutcome(
            ok=False, answer=fallback_for(question, text), reasons=reasons
        )

    # Không fail vì mấy lỗi nhỏ này, chỉ cắt gọn lại cho đúng hợp đồng với FE.
    #
    # `should_see_doctor` thì chỉ BẬT thêm, không bao giờ tắt: câu trả lời nói
    # tới dấu hiệu cấp cứu mà cờ vẫn false là model quên bật, và hướng sai duy
    # nhất đáng sợ ở đây là bỏ sót. Ngược lại (cờ true mà nội dung không có dấu
    # hiệu nào) thì để nguyên — model có thể có lý do mình không thấy.
    escalate = not answer.should_see_doctor and mentions_emergency(text)
    if escalate:
        # Đếm riêng: con số này tăng đều nghĩa là prompt đang dạy model chưa đủ
        # rõ về cờ cấp cứu, và đó là thứ phải sửa ở prompt chứ không phải ở đây.
        metrics.incr("emergency_flag_forced")
    cleaned = answer.model_copy(
        update={
            "answer": text,
            "follow_up_questions": follow_ups[:MAX_FOLLOW_UPS],
            "should_see_doctor": answer.should_see_doctor or escalate,
        }
    )
    return ValidationOutcome(ok=True, answer=cleaned)
