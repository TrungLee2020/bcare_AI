"""
Bước 1: câu gõ tự do → thẻ triệu chứng chuẩn (symptom_tags.json).

LLM chỉ được trả key nằm trong danh sách thẻ (enum trong schema strict), và mỗi
key phải kèm đoạn chữ của chính người dùng làm căn cứ. Căn cứ không có trong
câu gõ thì bỏ key đó: model không được thêm triệu chứng người dùng không nói.

Dấu hiệu cấp cứu còn được dò bằng từ khoá, chạy cả khi LLM lỗi. Hai nguồn hợp
lại: một trong hai thấy là cấp cứu. Từ khoá không hiểu phủ định ("không méo
miệng" vẫn khớp) — chấp nhận, vì báo nhầm cấp cứu ít hại hơn bỏ sót.
"""

import enum
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model

from app.services.facility import catalog
from app.services.input_filter import normalize

EMERGENCY_KINDS = ("stroke", "chest_pain", "breathing", "head_injury")

# Viết ở dạng đã bỏ dấu (normalize()). Chỉ những cụm đủ đặc hiệu.
_EMERGENCY_PATTERNS: list[tuple[str, str]] = [
    ("stroke", r"\bdot quy\b|\btai bien\b|\bmeo mieng\b|\blech mieng\b|\b(yeu|te|liet) (nua nguoi|mot ben)\b"
               r"|\bnua nguoi\b.{0,15}\b(yeu|te|liet)\b|\bnoi (do|dot|ngong|lap)\b|\bkhong noi duoc\b"),
    ("chest_pain", r"\bdau (that )?nguc\b.{0,40}\b(du doi|lan (ra |xuong )?(tay|ham|vai)|va mo hoi)\b"),
    ("breathing", r"\b(kho tho|ngop tho|tho khong noi)\b.{0,30}\b(du doi|tim moi|moi tim|khong noi (duoc|thanh cau))\b"
                  r"|\btim tai\b|\btim moi\b|\bmoi tim\b"),
    ("head_injury", r"\b(chan thuong|dap|va|te|nga)\b.{0,30}\bdau\b.{0,40}\b(chay mau (tai|mui)|lo mo|ngat|bat tinh|non)\b"),
]
_COMPILED_EMERGENCY = [(kind, re.compile(p)) for kind, p in _EMERGENCY_PATTERNS]


def emergency_from_keywords(text: str) -> str:
    normalized = normalize(text)
    for kind, pattern in _COMPILED_EMERGENCY:
        if pattern.search(normalized):
            return kind
    return ""


def _symptom_enum() -> type[enum.Enum]:
    keys = list(catalog.all_symptoms())
    return enum.Enum("SymptomKey", {k: k for k in keys}, type=str)


def _guess_enum() -> type[enum.Enum]:
    keys = ["", *catalog.guessable_specialties()]
    return enum.Enum("GuessSpecialty", {(k or "none"): k for k in keys}, type=str)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _build_schema() -> type[BaseModel]:
    found = create_model(
        "FoundSymptom",
        __base__=_Base,
        key=(_symptom_enum(), ...),
        matched_text=(str, ...),
    )
    unmapped = create_model(
        "UnmappedPhrase",
        __base__=_Base,
        text=(str, ...),
        specialty=(_guess_enum(), ...),
        reason=(str, ...),
    )
    return create_model(
        "SymptomInterpretation",
        __base__=_Base,
        symptoms=(list[found], ...),
        unmapped=(list[unmapped], ...),
        after_trauma=(bool, ...),
        mental_health=(bool, ...),
        ambiguous_neuro=(bool, ...),
        emergency=(Literal["", "stroke", "chest_pain", "breathing", "head_injury"], ...),
    )


SymptomInterpretation = _build_schema()


class Interpretation(BaseModel):
    """Kết quả đã kiểm, dùng trong service (không phải schema gửi model)."""

    symptoms: list[tuple[str, str]] = []  # (key, matched_text)
    unmapped: list[str] = []
    guesses: list[tuple[str, str, str]] = []  # (text, key chuyên khoa, lý do)
    after_trauma: bool = False
    mental_health: bool = False
    ambiguous_neuro: bool = False
    emergency: str = ""


MAX_UNMAPPED = 5
MAX_REASON_CHARS = 80
MAX_PHRASE_CHARS = 60


def _grounded(phrase: str, text_norm: str) -> bool:
    phrase_norm = normalize(phrase)
    return bool(phrase_norm) and phrase_norm in text_norm


def verify(raw: BaseModel, text: str) -> Interpretation:
    """Giữ lại những gì có căn cứ trong câu gõ, rồi đối chiếu nguyên chữ."""
    text_norm = normalize(text)
    symptoms: list[tuple[str, str]] = []

    def add(key: str, matched: str) -> None:
        if key not in (k for k, _ in symptoms):
            symptoms.append((key, matched.strip()[:MAX_PHRASE_CHARS]))

    for item in raw.symptoms:
        key = item.key.value if isinstance(item.key, enum.Enum) else str(item.key)
        if key not in catalog.all_symptoms() or not _grounded(item.matched_text, text_norm):
            continue
        # Căn cứ của model khớp trọn một cụm trong bảng mà không phải của key
        # model chọn → tin bảng ("dau da day" là đau bụng, không phải đau đầu;
        # "bong gân cổ chân" là chấn thương, không phải đau khớp). Căn cứ không
        # khớp cụm nào (model hiểu theo nghĩa) thì giữ key của model.
        literal = catalog.literal_matches(item.matched_text)
        if literal and key not in (k for k, _ in literal):
            for lit_key, lit_text in literal:
                add(lit_key, lit_text)
        else:
            add(key, item.matched_text)

    # Cụm khớp trọn tên / từ đồng nghĩa mà model bỏ sót.
    for key, matched in catalog.literal_matches(text):
        add(key, matched)

    matched_norm = [normalize(m) for _, m in symptoms]
    unmapped = []
    guesses: list[tuple[str, str, str]] = []
    for item in raw.unmapped:
        phrase = item.text
        phrase_norm = normalize(phrase)
        if not _grounded(phrase, text_norm):
            continue
        # Đã có trong bảng, đã nằm trong một triệu chứng đã nhận, hoặc đã được
        # xử lý bằng cờ: không phải "chưa map", không log, không hiện ghi chú.
        if catalog.literal_matches(phrase):
            continue
        if any(phrase_norm in m or m in phrase_norm for m in matched_norm):
            continue
        if raw.ambiguous_neuro and "than kinh" in phrase_norm:
            continue
        text_kept = phrase.strip()[:MAX_PHRASE_CHARS]
        unmapped.append(text_kept)
        specialty = item.specialty.value if isinstance(item.specialty, enum.Enum) else str(item.specialty)
        # Chỉ cụm được giữ mới tạo giả định; tam_than / nhi không bao giờ được giả định.
        if specialty and specialty in catalog.guessable_specialties():
            # Không có lý do thì lấy chính cụm chữ làm lý do.
            reason = (item.reason.strip() or text_kept)[:MAX_REASON_CHARS]
            guesses.append((text_kept, specialty, reason))
    kept = unmapped[:MAX_UNMAPPED]
    return Interpretation(
        symptoms=symptoms,
        unmapped=kept,
        guesses=[g for g in guesses if g[0] in kept],
        after_trauma=raw.after_trauma,
        mental_health=raw.mental_health,
        ambiguous_neuro=raw.ambiguous_neuro,
        emergency=raw.emergency,
    )


def tag_table() -> str:
    """Danh sách triệu chứng đưa vào system prompt: key, tên, từ đồng nghĩa."""
    lines = []
    for key, tag in catalog.all_symptoms().items():
        synonyms = ", ".join(tag.get("synonyms", []))
        lines.append(f"- {key}: {tag['name']} ({synonyms})")
    return "\n".join(lines)


def specialty_table() -> str:
    """Danh sách chuyên khoa được phép giả định, đưa vào system prompt."""
    lines = []
    for key in catalog.guessable_specialties():
        detail = catalog.specialty_details()[key]
        typical = detail.get("typical_symptoms", [])
        if not isinstance(typical, str):
            typical = ", ".join(typical)
        lines.append(f"- {key}: {detail['label']} (triệu chứng điển hình: {typical})")
    return "\n".join(lines)
