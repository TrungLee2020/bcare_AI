"""
Điều phối 4 bước cho `POST /v1/facility/suggest`.

  1. Chuẩn hoá: thẻ app gửi + thẻ đọc từ câu gõ (LLM, đã kiểm căn cứ).
  2. Map sang chuyên khoa: catalog.map_symptoms (chỉ qua bảng map).
  3–4. Lọc cứng + xếp hạng: ranking.rank (code cố định).

Thứ tự ưu tiên của trạng thái: emergency → clarify → ok / empty.
"""

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis

from app import metrics
from app.config import settings
from app.prompts import load_prompt
from app.services import cost, input_filter, openai_client
from app.services.facility import catalog, facilities, ranking
from app.services.facility.interpret import (
    Interpretation,
    SymptomInterpretation,
    emergency_from_keywords,
    specialty_table,
    tag_table,
    verify,
)

logger = logging.getLogger(__name__)

GENERAL_ONLY = "general_only"
RADIUS_STEPS_KM = (10.0, 25.0, 50.0)

EMERGENCY_MESSAGES = {
    "stroke": "Méo miệng, yếu hoặc tê nửa người, nói đớ có thể là dấu hiệu đột quỵ. "
              "Gọi 115 ngay hoặc đến cơ sở cấp cứu gần nhất.",
    "chest_pain": "Đau ngực dữ dội, lan ra tay hoặc hàm, vã mồ hôi cần được cấp cứu ngay. "
                  "Gọi 115 hoặc đến cơ sở cấp cứu gần nhất.",
    "breathing": "Khó thở dữ dội, tím môi cần được cấp cứu ngay. "
                 "Gọi 115 hoặc đến cơ sở cấp cứu gần nhất.",
    "head_injury": "Chảy máu tai hoặc mũi, lơ mơ, ngất sau chấn thương đầu cần được cấp cứu ngay. "
                   "Gọi 115 hoặc đến cơ sở cấp cứu gần nhất.",
}

CLARIFY_NEURO = {
    "question": "Bạn đang gặp vấn đề nào?",
    "options": [
        {"value": "than_kinh", "label": "Triệu chứng cơ thể: đau đầu, chóng mặt, tê bì…"},
        {"value": "tam_than", "label": "Tinh thần: lo âu, buồn bã kéo dài, căng thẳng…"},
        {"value": GENERAL_ONLY, "label": "Chưa rõ, gợi ý đa khoa"},
    ],
}


class InvalidRequest(ValueError):
    pass


class LimitReached(Exception):
    """Hết số lần đọc câu gõ bằng model trong ngày (FACILITY_MAX_PER_DAY)."""


async def _free() -> None:
    return None


def _system_prompt() -> str:
    return (
        load_prompt("facility", settings.facility_prompt_version)
        + "\n"
        + tag_table()
        + "\n\nDanh sách chuyên khoa (key: tên (triệu chứng điển hình)):\n"
        + specialty_table()
    )


def _cache_key(text: str) -> str:
    digest = hashlib.sha256(text.strip().encode()).hexdigest()
    return f"facility_interp:{settings.facility_prompt_version}:{digest}"


async def _interpret(
    redis: Redis,
    text: str,
    charge: Callable[[], Awaitable[None]],
    refund: Callable[[], Awaitable[None]],
) -> Interpretation | None:
    """None = không đọc được (LLM lỗi hoặc quá FACILITY_LLM_BUDGET_SECONDS).
    Câu bị lọc injection coi như câu rỗng. Chỉ lần gọi model thật mới bị tính
    lượt (`charge`, ném LimitReached khi hết); lỗi thì hoàn (`refund`)."""
    if input_filter.check(text).blocked:
        metrics.incr("facility_text_blocked")
        return Interpretation()
    key = _cache_key(text)
    cached = await redis.get(key)
    if cached:
        metrics.incr("facility_interp_cached")
        return Interpretation.model_validate_json(cached)
    await charge()
    try:
        raw, usage = await asyncio.wait_for(
            openai_client.interpret_symptoms(text, _system_prompt(), SymptomInterpretation),
            settings.facility_llm_budget_seconds,
        )
    except Exception:
        await refund()
        metrics.incr("facility_interp_failed")
        logger.exception("Đọc câu gõ triệu chứng thất bại")
        return None
    cost.record(usage, settings.openai_model)
    result = verify(raw, text)
    await redis.set(key, result.model_dump_json(), ex=settings.facility_interpret_cache_seconds)
    return result


def _symptom_view(key: str, source: str, matched_text: str | None = None) -> dict:
    view = {"key": key, "name": catalog.symptom_name(key), "source": source}
    if matched_text:
        view["matched_text"] = matched_text
    return view


def _facility_view(r: ranking.Ranked) -> dict:
    f = r.facility
    return {
        "id": f.id,
        "name": f.name,
        "lat": f.lat,
        "lng": f.lng,
        "address": f.address,
        "city": f.city,
        "distance_km": r.distance_km,
        "match": r.match,
        "matched_specialties": list(r.matched_specialties),
        "audience": f.audience,
        "reason": r.reason,
    }


def _next_radius(radius: float) -> float | None:
    cap = settings.facility_max_radius_km
    for step in RADIUS_STEPS_KM:
        if radius < step <= cap:
            return step
    return cap if radius < cap else None


def _fmt_km(value: float) -> str:
    return f"{value:g}"


async def suggest(
    redis: Redis,
    *,
    age: int | None,
    symptom_tags: list[str],
    text: str | None,
    lat: float,
    lng: float,
    radius_km: float | None,
    limit: int | None,
    clarify_choice: str | None,
    charge: Callable[[], Awaitable[None]] = _free,
    refund: Callable[[], Awaitable[None]] = _free,
) -> dict:
    tags = catalog.symptom_tags()
    unknown = [t for t in symptom_tags if t not in tags]
    if unknown:
        raise InvalidRequest(f"Thẻ triệu chứng không hợp lệ: {', '.join(unknown[:5])}")
    text = (text or "").strip()
    if not symptom_tags and not text:
        raise InvalidRequest("Cần chọn thẻ triệu chứng hoặc mô tả triệu chứng.")
    if len(text) > settings.facility_text_max_chars:
        raise InvalidRequest(f"Mô tả triệu chứng dài quá {settings.facility_text_max_chars} ký tự.")
    if clarify_choice and clarify_choice != GENERAL_ONLY and clarify_choice not in catalog.specialties():
        raise InvalidRequest("Lựa chọn không hợp lệ.")

    radius = min(radius_km or settings.facility_default_radius_km, settings.facility_max_radius_km)
    limit = min(limit or settings.facility_default_limit, settings.facility_max_limit)
    group = catalog.age_group(age)

    # Bước 1
    found = Interpretation()
    note = None
    if text:
        result = await _interpret(redis, text, charge, refund)
        if result is None:
            note = "Bụt chưa đọc được phần mô tả, đang gợi ý theo thẻ bạn chọn." if symptom_tags \
                else "Bụt chưa đọc được phần mô tả, đang gợi ý chung."
        else:
            found = result
    emergency = emergency_from_keywords(text) if text else ""
    emergency = emergency or found.emergency

    symptoms = [_symptom_view(k, "tag") for k in dict.fromkeys(symptom_tags)]
    seen = set(symptom_tags)
    for key, matched in found.symptoms:
        if key not in seen:
            symptoms.append(_symptom_view(key, "text", matched))
            seen.add(key)
    symptom_keys = [s["key"] for s in symptoms]

    # Bước 2
    general_only = clarify_choice == GENERAL_ONLY
    mapping = catalog.map_symptoms(
        [] if general_only else symptom_keys,
        group,
        after_trauma=found.after_trauma,
        mental_health=found.mental_health and not clarify_choice,
        guesses=[] if general_only else found.guesses,
    )
    if clarify_choice and not general_only:
        mapping.specialties.setdefault(clarify_choice, catalog.SpecialtyPick(clarify_choice, "primary", []))
        pick = mapping.specialties[clarify_choice]
        pick.role = "primary"
        # Người dùng chọn thắng giả định của model.
        pick.assumed = False
        pick.reason = ""
        pick.sources = [s for s in pick.sources if s != "assumed"]
        if "choice" not in pick.sources:
            pick.sources.append("choice")

    if found.unmapped:
        metrics.incr("facility_unmapped")
        guess_by_text = {text: key for text, key, _ in found.guesses}
        for phrase in found.unmapped:
            guess = guess_by_text.get(phrase, "none")
            metrics.incr(f"facility_guess_{guess}")
            # Không kèm user id, không kèm toạ độ.
            logger.info("facility_unmapped phrase=%r guess=%s group=%s", phrase, guess, group)

    assumed_labels = [catalog.specialty_label(p.key) for p in mapping.specialties.values() if p.assumed]
    if note is None:
        if general_only:
            note = "Đang gợi ý đa khoa theo lựa chọn của bạn."
        elif not mapping.specialties:
            note = "Đang gợi ý chung (đa khoa) vì chưa đủ thông tin để chọn chuyên khoa."
        elif assumed_labels:
            note = ("Một phần mô tả chưa có trong bảng; Bụt tạm đoán chuyên khoa "
                    + ", ".join(assumed_labels) + ", kèm cơ sở đa khoa để bạn chọn.")
        elif found.unmapped:
            note = "Một phần mô tả chưa có trong bảng triệu chứng; đang gợi ý theo phần còn lại."

    interpretation = {
        "age_group": group,
        "symptoms": symptoms,
        "specialties": [
            {"key": p.key, "label": catalog.specialty_label(p.key), "role": p.role, "from": p.sources,
             "assumed": p.assumed, "reason": p.reason}
            for p in mapping.specialties.values()
        ],
        "fallback_general": True,
        "unmapped_text": found.unmapped,
        "note": note,
    }

    snapshot = await facilities.get()
    base = {"interpretation": interpretation, "data_version": snapshot.version}

    if emergency:
        metrics.incr(f"facility_emergency_{emergency}")
        nearest = ranking.nearest_general(
            snapshot.facilities, group, lat, lng, settings.facility_emergency_count
        )
        return {
            "status": "emergency",
            **base,
            "emergency": {"kind": emergency, "message": EMERGENCY_MESSAGES[emergency], "call": "115"},
            "facilities": [_facility_view(r) for r in nearest],
        }

    if found.ambiguous_neuro and not clarify_choice:
        metrics.incr("facility_clarify")
        return {"status": "clarify", **base, "clarify": CLARIFY_NEURO, "facilities": []}

    ranked = ranking.rank(snapshot.facilities, mapping, group, lat, lng, radius, limit)
    if ranked:
        metrics.incr("facility_ok")
        return {"status": "ok", **base, "facilities": [_facility_view(r) for r in ranked]}

    metrics.incr("facility_empty")
    labels = [p["label"] for p in interpretation["specialties"]]
    wanted = " hoặc ".join([*labels, "đa khoa"]) if labels else "đa khoa"
    who = "trẻ em" if group == "child" else "người lớn"
    suggestions = []
    wider = _next_radius(radius)
    if wider:
        suggestions.append({"kind": "widen_radius", "radius_km": wider, "label": f"Tìm trong {_fmt_km(wider)} km"})
    return {
        "status": "empty",
        **base,
        "facilities": [],
        "empty": {
            "message": f"Chưa tìm thấy nơi khám {wanted} cho {who} trong {_fmt_km(radius)} km.",
            "suggestions": suggestions,
        },
    }
