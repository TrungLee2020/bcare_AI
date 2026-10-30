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
    keys = list(catalog.symptom_tags())
    return enum.Enum("SymptomKey", {k: k for k in keys}, type=str)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _build_schema() -> type[BaseModel]:
    found = create_model(
        "FoundSymptom",
        __base__=_Base,
        key=(_symptom_enum(), ...),
        matched_text=(str, ...),
    )
    return create_model(
        "SymptomInterpretation",
        __base__=_Base,
        symptoms=(list[found], ...),
        unmapped=(list[str], ...),
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
    after_trauma: bool = False
    mental_health: bool = False
    ambiguous_neuro: bool = False
    emergency: str = ""


MAX_UNMAPPED = 5
MAX_PHRASE_CHARS = 60


def _grounded(phrase: str, text_norm: str) -> bool:
    phrase_norm = normalize(phrase)
    return bool(phrase_norm) and phrase_norm in text_norm


def verify(raw: BaseModel, text: str) -> Interpretation:
    """Giữ lại những gì có căn cứ trong câu gõ."""
    text_norm = normalize(text)
    symptoms: list[tuple[str, str]] = []
    for item in raw.symptoms:
        key = item.key.value if isinstance(item.key, enum.Enum) else str(item.key)
        if key in catalog.symptom_tags() and _grounded(item.matched_text, text_norm):
            if key not in (k for k, _ in symptoms):
                symptoms.append((key, item.matched_text.strip()[:MAX_PHRASE_CHARS]))
    unmapped = [
        p.strip()[:MAX_PHRASE_CHARS]
        for p in raw.unmapped
        if _grounded(p, text_norm)
    ][:MAX_UNMAPPED]
    return Interpretation(
        symptoms=symptoms,
        unmapped=unmapped,
        after_trauma=raw.after_trauma,
        mental_health=raw.mental_health,
        ambiguous_neuro=raw.ambiguous_neuro,
        emergency=raw.emergency,
    )


def tag_table() -> str:
    """Danh sách thẻ đưa vào system prompt: key, tên, từ đồng nghĩa."""
    lines = []
    for key, tag in catalog.symptom_tags().items():
        synonyms = ", ".join(tag.get("synonyms", []))
        lines.append(f"- {key}: {tag['name']} ({synonyms})")
    return "\n".join(lines)
