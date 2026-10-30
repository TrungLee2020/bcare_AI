"""
Bước 3–4: lọc cứng rồi xếp hạng. Code cố định, không có LLM.

Lọc cứng (giữ cơ sở thoả ĐỒNG THỜI):
  - có chuyên khoa đã map, hoặc là đa khoa (dự phòng);
  - đúng đối tượng: người lớn bỏ cơ sở chỉ khám trẻ em, trẻ em bỏ cơ sở chỉ
    khám người lớn;
  - trong bán kính.
Xếp hạng: mức khớp (chính → phụ → [trẻ em: cơ sở Nhi bất kỳ] → đa khoa), trẻ em
ưu tiên cơ sở Nhi trong cùng mức, rồi gần hơn trước.
"""

import math
from dataclasses import dataclass

from app.services.facility import catalog
from app.services.facility.catalog import Mapping
from app.services.facility.facilities import Facility

EARTH_RADIUS_KM = 6371.0
_LEVEL = {"primary": 0, "secondary": 1, "pediatric": 2, "general": 3}


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class Ranked:
    facility: Facility
    distance_km: float
    match: str  # primary | secondary | pediatric | general
    matched_specialties: tuple[str, ...]
    reason: str


def audience_fits(facility: Facility, group: str) -> bool:
    if group == "adult":
        return facility.audience != "child"
    return facility.audience != "adult"


def _audience_text(facility: Facility, group: str) -> str:
    if group == "adult":
        return "Phù hợp: người lớn"
    if facility.audience == "child":
        return "Phù hợp: trẻ em"
    return "Phù hợp: trẻ em và người lớn"


def _symptom_text(sources: list[str]) -> str:
    names = []
    for source in sources:
        if source == "mental":
            names.append("Sức khỏe tinh thần")
        elif source == "choice":
            names.append("Bạn chọn")
        else:
            names.append(catalog.symptom_name(source))
    return ", ".join(names)


def _match(facility: Facility, mapping: Mapping, group: str) -> tuple[str, tuple[str, ...]] | None:
    hits = [p for p in mapping.specialties.values() if p.key in facility.specialties]
    if hits:
        level = "primary" if any(p.role == "primary" for p in hits) else "secondary"
        return level, tuple(p.key for p in hits)
    if group == "child" and facility.pediatric:
        return "pediatric", ()
    if facility.is_general:
        return "general", ()
    return None


def _reason(facility: Facility, mapping: Mapping, level: str, keys: tuple[str, ...], group: str) -> str:
    if keys:
        parts = [
            f"{_symptom_text(mapping.specialties[k].sources)} → {catalog.specialty_label(k)}"
            for k in keys
        ]
        head = "; ".join(parts)
    elif level == "pediatric":
        head = "Cơ sở Nhi: khám ban đầu cho trẻ"
    elif mapping.specialties:
        head = "Đa khoa: khám ban đầu, bác sĩ chuyển đúng chuyên khoa nếu cần"
    else:
        head = "Đa khoa: gợi ý chung vì chưa đủ thông tin để chọn chuyên khoa"
    return f"{head} · {_audience_text(facility, group)}"


def rank(
    facilities: tuple[Facility, ...] | list[Facility],
    mapping: Mapping,
    group: str,
    lat: float,
    lng: float,
    radius_km: float,
    limit: int,
) -> list[Ranked]:
    found: list[tuple[tuple, Ranked]] = []
    for facility in facilities:
        if not audience_fits(facility, group):
            continue
        distance = haversine_km(lat, lng, facility.lat, facility.lng)
        if distance > radius_km:
            continue
        matched = _match(facility, mapping, group)
        if matched is None:
            continue
        level, keys = matched
        ranked = Ranked(
            facility=facility,
            distance_km=round(distance, 1),
            match=level,
            matched_specialties=keys,
            reason=_reason(facility, mapping, level, keys, group),
        )
        pediatric_first = 0 if group == "child" and facility.pediatric else 1
        found.append(((_LEVEL[level], pediatric_first, distance, facility.id), ranked))
    found.sort(key=lambda item: item[0])
    return [ranked for _, ranked in found[:limit]]


def nearest_general(
    facilities: tuple[Facility, ...] | list[Facility],
    group: str,
    lat: float,
    lng: float,
    count: int,
) -> list[Ranked]:
    """Cho nhánh cấp cứu: cơ sở đa khoa gần nhất, không giới hạn bán kính.
    DB chưa có cột "có cấp cứu", nên đây chỉ là gần đúng."""
    found = []
    for facility in facilities:
        if not facility.is_general or not audience_fits(facility, group):
            continue
        distance = haversine_km(lat, lng, facility.lat, facility.lng)
        found.append((distance, facility.id, facility))
    found.sort(key=lambda item: item[:2])
    return [
        Ranked(
            facility=f,
            distance_km=round(d, 1),
            match="general",
            matched_specialties=(),
            reason=f"Cơ sở đa khoa gần nhất · {_audience_text(f, group)}",
        )
        for d, _, f in found[:count]
    ]
