"""
Dữ liệu tĩnh của tính năng gợi ý cơ sở: chuyên khoa, thẻ triệu chứng, bảng map
triệu chứng → chuyên khoa (app/facility_data/*.json).

Bước 2 của luồng (map sang chuyên khoa) nằm trọn ở đây và chỉ đi qua bảng map.
Không có nhánh nào suy chuyên khoa từ chữ giống nhau: triệu chứng không có
trong bảng thì ra đa khoa.
"""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from app.services.input_filter import normalize

DATA_DIR = Path(__file__).resolve().parents[2] / "facility_data"

# Bảng map dùng "da_khoa" để nói "đa khoa" — không phải chuyên khoa mà là cờ
# facilities.is_general.
GENERAL = "da_khoa"
CHILD_MAX_AGE = 16

# Có té ngã / va đập / tai nạn thì các triệu chứng này đi Ngoại chấn thương
# trước, chuyên khoa gốc lùi xuống phụ (cặp dễ nhầm co_xuong_khop /
# ngoai_chan_thuong, than_kinh / ngoại thần kinh).
TRAUMA_SENSITIVE = frozenset({"joint_pain", "back_pain", "headache", "numbness"})
TRAUMA_SPECIALTY = "ngoai_chan_thuong"
MENTAL_SPECIALTY = "tam_than"


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


@lru_cache
def specialties() -> dict[str, str]:
    """key → nhãn hiển thị."""
    return {s["key"]: s["label"] for s in _load("specialties.json")["specialties"]}


@lru_cache
def symptom_tags() -> dict[str, dict]:
    """key → {name, synonyms, group}."""
    return {t["key"]: t for t in _load("symptom_tags.json")["tags"]}


@lru_cache
def symptom_map() -> dict[str, dict]:
    return _load("symptom_specialty_map.json")["map"]


@lru_cache
def extra_symptoms() -> dict[str, dict]:
    """Triệu chứng ngoài 25 thẻ của app (đau răng, mờ mắt, trễ kinh…), kèm bảng
    map riêng. App không gửi các key này; chỉ bước đọc câu gõ trả ra."""
    return {s["key"]: s for s in _load("extra_symptoms.json")["symptoms"]}


@lru_cache
def all_symptoms() -> dict[str, dict]:
    """Thẻ của app + triệu chứng bổ sung: từ vựng của bước đọc câu gõ."""
    return {**symptom_tags(), **extra_symptoms()}


@lru_cache
def overrides() -> dict[str, dict]:
    return _load("facility_overrides.json")["overrides"]


def symptom_name(key: str) -> str:
    return all_symptoms()[key]["name"]


def _map_entry(symptom: str, group: str) -> dict:
    if symptom in extra_symptoms():
        return extra_symptoms()[symptom]["map"][group]
    return symptom_map()[symptom][group]


# --- Đối chiếu nguyên chữ ---------------------------------------------------
# Lưới an toàn cho bước đọc câu gõ bằng model: cụm khớp đúng tên / từ đồng
# nghĩa của một triệu chứng thì không được rơi vào "chưa map" (log production
# 2026-11-05: "Trầm cảm" bị log facility_unmapped dù đã gợi ý đúng Tâm thần;
# chạy thử: "sổ mũi", "ói" bị đẩy vào unmapped, "dau da day" bị hiểu là đau
# đầu). Đây vẫn là tra bảng, không phải đoán theo chữ gần giống: chỉ khớp trọn
# cụm đã liệt kê.
#
# - Ưu tiên cụm dài nhất tại mỗi vị trí: "đau đầu gối" là đau khớp, không phải
#   đau đầu.
# - Cụm một từ phải khớp cả dấu: "ho" không khớp "họ", "ói" không khớp "ơi".
# - Bỏ cụm đứng ngay sau từ phủ định ("không sốt", "hết ho").
_NEGATIONS = frozenset({"khong", "ko", "k", "kg", "khg", "het", "chua", "chang", "cha"})
_NEGATION_WINDOW = 3
_CLAUSE_BREAK = re.compile(r"[,.;:!?\n]")
_CLAUSE_WORDS = frozenset({"nhung", "ma"})
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _norm_word(word: str) -> str:
    return normalize(word)


@dataclass(frozen=True)
class _Phrase:
    key: str
    norm: tuple[str, ...]
    accented: tuple[str, ...]
    # Bỏ dấu thì trùng với cụm của triệu chứng khác ("khó tiểu" / "khó tiêu").
    collides: bool = False

    @property
    def accent_required(self) -> bool:
        return len(self.norm) == 1 or self.collides


@lru_cache
def _phrases() -> tuple[_Phrase, ...]:
    raw: dict[tuple[tuple[str, ...], tuple[str, ...]], str] = {}
    for key, item in all_symptoms().items():
        for text in [item["name"], *item.get("synonyms", [])]:
            words = tuple(_WORD_RE.findall(unicodedata.normalize("NFC", text.lower())))
            if words:
                raw.setdefault((tuple(_norm_word(w) for w in words), words), key)
    keys_by_norm: dict[tuple[str, ...], set[str]] = {}
    for (norm, _), key in raw.items():
        keys_by_norm.setdefault(norm, set()).add(key)
    out = [
        _Phrase(key, norm, accented, collides=len(keys_by_norm[norm]) > 1)
        for (norm, accented), key in raw.items()
    ]
    # Dài trước: tại mỗi vị trí lấy cụm dài nhất.
    return tuple(sorted(out, key=lambda p: len(p.norm), reverse=True))


def _negated(tokens: list[re.Match], norm: list[str], text: str, i: int) -> bool:
    """Có từ phủ định trong vài từ ngay trước, cùng vế câu? Vế câu dừng ở dấu
    câu và ở "nhưng", "mà": "không sốt nhưng ho" vẫn nhận ho."""
    j = i - 1
    while j >= 0 and j >= i - _NEGATION_WINDOW:
        if _CLAUSE_BREAK.search(text[tokens[j].end() : tokens[j + 1].start()]):
            return False
        if norm[j] in _CLAUSE_WORDS:
            return False
        if norm[j] in _NEGATIONS:
            return True
        j -= 1
    return False


def literal_matches(text: str) -> list[tuple[str, str]]:
    """(key, đoạn chữ nguyên văn) cho từng cụm khớp trọn tên / từ đồng nghĩa."""
    text = unicodedata.normalize("NFC", text)
    tokens = list(_WORD_RE.finditer(text))
    lower = [t.group().lower() for t in tokens]
    norm = [_norm_word(w) for w in lower]
    # Câu gõ không dấu thì cụm một từ không phân biệt được ("ho bao toi" là
    # "họ bảo tôi"): bỏ, để model đọc theo ngữ cảnh.
    has_accents = normalize(text) != text.lower()
    found: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        for p in _phrases():
            n = len(p.norm)
            if tuple(norm[i : i + n]) != p.norm:
                continue
            # Câu có dấu thì khớp cả dấu ("bị tiêu" không phải "bí tiểu"); câu
            # không dấu thì chỉ so dạng bỏ dấu, trừ cụm một từ (xem trên).
            if has_accents and tuple(lower[i : i + n]) != p.accented:
                continue
            if not has_accents and p.accent_required:
                continue
            if not _negated(tokens, norm, text, i):
                found.append((p.key, text[tokens[i].start() : tokens[i + n - 1].end()]))
            i += n
            break
        else:
            i += 1
    return found


def specialty_label(key: str) -> str:
    return specialties()[key]


def age_group(age: int | None) -> str:
    """None = người lớn chưa rõ tuổi."""
    return "child" if age is not None and age <= CHILD_MAX_AGE else "adult"


@dataclass
class SpecialtyPick:
    key: str
    role: str  # primary | secondary
    sources: list[str] = field(default_factory=list)  # symptom key, hoặc "mental" / "choice"


@dataclass
class Mapping:
    specialties: dict[str, SpecialtyPick]
    # Có triệu chứng mà bảng map chỉ trỏ tới đa khoa (vd. sốt ở người lớn).
    general_symptoms: list[str]


def map_symptoms(
    symptom_keys: list[str],
    group: str,
    *,
    after_trauma: bool = False,
    mental_health: bool = False,
) -> Mapping:
    picks: dict[str, SpecialtyPick] = {}
    general: list[str] = []

    def add(key: str, role: str, source: str) -> None:
        pick = picks.get(key)
        if pick is None:
            picks[key] = SpecialtyPick(key, role, [source])
            return
        if role == "primary":
            pick.role = "primary"
        if source not in pick.sources:
            pick.sources.append(source)

    for symptom in symptom_keys:
        entry = _map_entry(symptom, group)
        primary = list(entry["primary"])
        secondary = list(entry["secondary"])
        if after_trauma and symptom in TRAUMA_SENSITIVE:
            secondary = [k for k in primary + secondary if k != GENERAL]
            primary = [TRAUMA_SPECIALTY]
        if GENERAL in primary:
            general.append(symptom)
        for key in primary:
            if key != GENERAL:
                add(key, "primary", symptom)
        for key in secondary:
            if key != GENERAL:
                add(key, "secondary", symptom)

    if mental_health:
        add(MENTAL_SPECIALTY, "primary", "mental")

    # Chuyên khoa vừa chính (từ triệu chứng này) vừa phụ (từ triệu chứng kia)
    # giữ vai chính; add() đã nâng. Thứ tự: chính trước.
    ordered = dict(sorted(picks.items(), key=lambda kv: kv[1].role != "primary"))
    return Mapping(specialties=ordered, general_symptoms=general)
