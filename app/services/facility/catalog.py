"""
Dữ liệu tĩnh của tính năng gợi ý cơ sở: chuyên khoa, thẻ triệu chứng, bảng map
triệu chứng → chuyên khoa (app/facility_data/*.json).

Bước 2 của luồng (map sang chuyên khoa) nằm trọn ở đây và chỉ đi qua bảng map.
Không có nhánh nào suy chuyên khoa từ chữ giống nhau: triệu chứng không có
trong bảng thì ra đa khoa.
"""

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

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
def overrides() -> dict[str, dict]:
    return _load("facility_overrides.json")["overrides"]


def symptom_name(key: str) -> str:
    return symptom_tags()[key]["name"]


def specialty_label(key: str) -> str:
    return specialties()[key]


def age_group(age: int) -> str:
    return "child" if age <= CHILD_MAX_AGE else "adult"


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
        entry = symptom_map()[symptom][group]
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
