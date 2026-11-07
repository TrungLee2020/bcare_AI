"""Gợi ý cơ sở khám theo triệu chứng (KAN-115)."""

import json
import logging
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import v1
from app.config import settings
from app.redis_client import set_redis
from app.services import openai_client
from app.services.facility import catalog, facilities, ranking
from app.services.facility.facilities import Facility, clean
from app.services.facility.interpret import SymptomInterpretation, emergency_from_keywords, verify
from tests.test_v1_api import JWT_SECRET, SUPABASE_URL, auth

# Toạ độ quanh Q1 TP.HCM
HOME = (10.776, 106.700)
NHI_DONG_2 = "b1676f03-ec80-4fcd-bd2b-1efb603812c7"
TAM_THAN_HCM = "14904186-e89a-4b2a-b408-be8e8a3ec770"
NHI_TW = "81d1d15d-8739-47b5-a191-df12e56edeb0"
PHAP_Y = "990eb7da-0458-4e4b-a3df-a0444ac2b09c"
MEDLATEC = "2c7c486d-6c6f-46b6-ad3b-0ae479d1db3e"


def row(id, name, specialties=(), is_general=False, lat=HOME[0], lng=HOME[1], **extra):
    return {"id": id, "name": name, "lat": lat, "lng": lng, "specialties": list(specialties),
            "is_general": is_general, "updated_at": "2026-10-27T00:00:00Z", **extra}


# Đúng hai lỗi trong KAN-115: Nhi đồng 2 (tag than_kinh) và BV Tâm thần (tag
# than_kinh) đứng gần nhất.
ROWS = [
    row(NHI_DONG_2, "Bệnh viện Nhi đồng 2", ["nhi", "than_kinh", "ho_hap", "tieu_hoa"], lat=10.781, lng=106.703),
    row(TAM_THAN_HCM, "Bệnh viện Tâm thần Thành phố Hồ Chí Minh", ["tam_than", "than_kinh"], lat=10.777, lng=106.701),
    row("neuro-1", "Bệnh viện Đại học Y Dược", ["than_kinh", "tim_mach"], lat=10.755, lng=106.664),
    row("gen-1", "Bệnh viện Quận 1", is_general=True, lat=10.770, lng=106.695),
    row("gen-far", "Bệnh viện Đa khoa Bình Dương", is_general=True, lat=10.98, lng=106.65),
    row("untagged", "Bác sĩ Cường"),
    row("ped-clinic", "Phòng khám Nhi Sài Gòn", ["nhi"], lat=10.779, lng=106.699),
    row("san-nhi", "Bệnh viện Sản Nhi Quận 3", ["san", "nhi"], lat=10.783, lng=106.690),
]


def snapshot_of(rows):
    return tuple(f for f in (clean(r) for r in rows) if f is not None)


# --- Bảng map ---------------------------------------------------------------

def test_headache_maps_to_neurology_never_psychiatry():
    mapping = catalog.map_symptoms(["headache", "dizziness"], "adult")
    assert list(mapping.specialties) == ["than_kinh"]
    assert mapping.specialties["than_kinh"].role == "primary"
    assert mapping.specialties["than_kinh"].sources == ["headache", "dizziness"]


def test_trauma_moves_pain_to_trauma_surgery():
    mapping = catalog.map_symptoms(["back_pain"], "adult", after_trauma=True)
    assert mapping.specialties["ngoai_chan_thuong"].role == "primary"
    assert mapping.specialties["co_xuong_khop"].role == "secondary"


def test_psychiatry_only_when_user_says_so():
    assert "tam_than" not in catalog.map_symptoms(["insomnia"], "adult").specialties
    assert catalog.map_symptoms(["insomnia"], "adult", mental_health=True).specialties["tam_than"].role == "primary"


def test_general_only_symptom_has_no_specialty():
    mapping = catalog.map_symptoms(["chills"], "adult")
    assert mapping.specialties == {}
    assert mapping.general_symptoms == ["chills"]


def test_primary_wins_over_secondary():
    # abdominal_pain: tieu_hoa chính, ngoai_tong_quat phụ
    mapping = catalog.map_symptoms(["abdominal_pain"], "adult")
    assert mapping.specialties["tieu_hoa"].role == "primary"
    assert mapping.specialties["ngoai_tong_quat"].role == "secondary"


def test_age_group_boundary():
    assert catalog.age_group(16) == "child"
    assert catalog.age_group(17) == "adult"
    assert catalog.age_group(None) == "adult"


def test_adult_fever_adds_infectious_disease_as_secondary():
    mapping = catalog.map_symptoms(["fever"], "adult")
    assert mapping.general_symptoms == ["fever"]
    assert mapping.specialties["truyen_nhiem"].role == "secondary"


def test_every_map_entry_uses_known_keys():
    known = set(catalog.specialties()) | {catalog.GENERAL}
    assert set(catalog.symptom_map()) == set(catalog.symptom_tags())
    for entry in catalog.symptom_map().values():
        for group in ("adult", "child"):
            assert set(entry[group]["primary"]) | set(entry[group]["secondary"]) <= known


# --- Làm sạch dữ liệu cơ sở -------------------------------------------------

def test_overrides_fix_known_bad_rows():
    by_id = {f.id: f for f in snapshot_of(ROWS)}
    assert by_id[NHI_DONG_2].audience == "child"
    assert "than_kinh" not in by_id[TAM_THAN_HCM].specialties
    assert "untagged" not in by_id


def test_wrongly_general_hospitals_lose_general_flag():
    nhi_tw = clean(row(NHI_TW, "Bệnh viện Nhi Trung ương", is_general=True))
    assert nhi_tw.is_general is False
    assert nhi_tw.specialties == {"nhi"}
    assert nhi_tw.audience == "child"
    # Xét nghiệm: không có khoa phù hợp → ẩn
    assert clean(row(MEDLATEC, "PHÒNG KHÁM CHUYÊN KHOA XÉT NGHIỆM MEDLATEC AN GIANG", is_general=True)) is None
    assert clean(row(PHAP_Y, "Viện Pháp y Tâm thần Trung ương Biên Hòa", is_general=True)) is None


def test_name_rules_catch_rows_missing_from_override_list():
    psych = clean(row("x1", "Bệnh viện Tâm thần tỉnh Lâm Đồng", ["than_kinh"], is_general=True))
    assert psych.specialties == {"tam_than"} and psych.is_general is False
    assert clean(row("x2", "Phòng khám Nhi Hạnh Phúc", ["nhi"])).audience == "child"
    assert clean(row("x3", "Bệnh viện Sản Nhi Cà Mau", ["san", "nhi"])).audience == "all"
    assert clean(row("x4", "Bệnh viện Trẻ em Cần Thơ", ["nhi"])).audience == "child"
    # "Nhị" có dấu không phải Nhi khoa
    assert clean(row("x5", "Phòng khám Nhị Hà", is_general=True)).audience == "all"


def test_vaccination_and_preventive_centres_are_not_general():
    assert clean(row("x7", "Trung Tâm Y Tế Dự Phòng - Phòng Khám Tiêm Phòng", is_general=True)) is None


def test_tropical_disease_hospital_gets_new_key():
    f = clean(row("9d503159-d61e-4136-8d97-fafa01e41bfe", "Bệnh viện Bệnh Nhiệt đới Trung ương", is_general=True))
    assert f.specialties == {"truyen_nhiem"} and f.is_general is False


def test_db_is_trusted_once_migration_ran(monkeypatch):
    monkeypatch.setattr(settings, "facility_audience_column", True)
    # Không còn dùng danh sách sửa tạm: DB nói sao dùng vậy
    nhi_tw = clean(row(NHI_TW, "Bệnh viện Nhi Trung ương", ["nhi"], audience="child"))
    assert nhi_tw.audience == "child"
    assert clean(row(NHI_TW, "Bệnh viện Nhi Trung ương", ["nhi"], audience="all")).audience == "all"
    binh_dan = clean(row("bd", "Bệnh viện Bình Dân", ["ngoai_tong_quat"], audience="all"))
    assert binh_dan.specialties == {"ngoai_tong_quat"} and not binh_dan.is_general
    # Luật chỉ-bớt theo tên vẫn chạy
    assert "than_kinh" not in clean(row("x", "Bệnh viện Tâm thần X", ["than_kinh"], audience="all")).specialties
    assert clean(row(PHAP_Y, "Viện Pháp y Tâm thần", ["tam_than"], audience="adult")) is None


def test_unknown_specialty_keys_are_dropped():
    assert clean(row("x6", "PK lạ", ["khong_ton_tai"])) is None


# --- Lọc cứng và xếp hạng ---------------------------------------------------

def test_kan115_case_adult_headache():
    """Nữ 36 tuổi, đau đầu / chóng mặt: không Nhi đồng 2, không BV Tâm thần."""
    mapping = catalog.map_symptoms(["headache", "dizziness"], "adult")
    ranked = ranking.rank(snapshot_of(ROWS), mapping, "adult", *HOME, radius_km=10, limit=10)
    ids = [r.facility.id for r in ranked]
    assert NHI_DONG_2 not in ids
    assert TAM_THAN_HCM not in ids
    assert "ped-clinic" not in ids
    assert ids == ["neuro-1", "gen-1"]
    assert ranked[0].match == "primary"
    assert ranked[0].reason == "Đau đầu, Chóng mặt → Nội thần kinh · Phù hợp: người lớn"
    assert ranked[1].match == "general"


def test_child_prefers_pediatric_hospitals():
    mapping = catalog.map_symptoms(["headache"], "child")
    ranked = ranking.rank(snapshot_of(ROWS), mapping, "child", *HOME, radius_km=10, limit=10)
    ids = [r.facility.id for r in ranked]
    assert ids[0] == NHI_DONG_2  # Nhi + đúng khoa
    assert ids.index("neuro-1") < ids.index("ped-clinic") < ids.index("gen-1")
    assert TAM_THAN_HCM not in ids


def test_adult_fever_puts_general_before_tropical_hospital():
    rows = [
        row("trop", "Bệnh viện Bệnh Nhiệt đới", ["truyen_nhiem"], lat=10.80, lng=106.70),
        row("gen-near", "Bệnh viện Quận 1", is_general=True, lat=10.777, lng=106.700),
    ]
    mapping = catalog.map_symptoms(["fever"], "adult")
    ranked = ranking.rank(snapshot_of(rows), mapping, "adult", *HOME, radius_km=10, limit=10)
    assert [r.facility.id for r in ranked] == ["gen-near", "trop"]
    assert ranked[0].reason == "Sốt → Đa khoa · Phù hợp: người lớn"
    assert ranked[1].match == "secondary"


def test_radius_and_limit():
    mapping = catalog.map_symptoms(["chills"], "adult")
    ranked = ranking.rank(snapshot_of(ROWS), mapping, "adult", *HOME, radius_km=10, limit=10)
    assert "gen-far" not in [r.facility.id for r in ranked]
    assert len(ranking.rank(snapshot_of(ROWS), mapping, "adult", *HOME, radius_km=100, limit=1)) == 1


def test_haversine():
    assert ranking.haversine_km(10.776, 106.700, 10.776, 106.700) == 0
    assert 3.5 < ranking.haversine_km(10.776, 106.700, 10.755, 106.672) < 4.0


# --- Đọc câu gõ ---------------------------------------------------------------

@pytest.mark.parametrize("text,kind", [
    ("bố tôi tự nhiên méo miệng, nói đớ", "stroke"),
    ("yeu nua nguoi ben trai tu sang", "stroke"),
    ("đau thắt ngực dữ dội lan ra tay trái", "chest_pain"),
    ("khó thở dữ dội, môi tím", "breathing"),
    ("té đập đầu xong chảy máu tai", "head_injury"),
    ("đau đầu chóng mặt mấy hôm nay", ""),
    ("đau ngực nhẹ khi ho", ""),
])
def test_emergency_keywords(text, kind):
    assert emergency_from_keywords(text) == kind


def interp(**overrides):
    base = dict(symptoms=[], unmapped=[], after_trauma=False, mental_health=False,
                ambiguous_neuro=False, emergency="")
    base.update(overrides)
    # Chuỗi thì đổi thành mục không có giả định chuyên khoa.
    base["unmapped"] = [
        {"text": u, "specialty": "", "reason": ""} if isinstance(u, str) else u for u in base["unmapped"]
    ]
    return SymptomInterpretation.model_validate(base)


def test_verify_drops_symptoms_without_grounding():
    raw = interp(
        symptoms=[{"key": "headache", "matched_text": "nhức đầu"}, {"key": "fever", "matched_text": "sốt cao"}],
        unmapped=["đau gót chân", "bịa thêm"],
    )
    result = verify(raw, "Nhuc dau va dau got chan")
    assert result.symptoms == [("headache", "nhức đầu")]
    assert result.unmapped == ["đau gót chân"]


# --- API ----------------------------------------------------------------------

class FakeSupabase:
    def __init__(self, rows):
        self.rows = rows
        self.fail = False
        self.full_loads = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(500, text="boom")
        assert request.headers["apikey"] == "anon"
        params = request.url.params
        if params["select"] == "updated_at":
            return httpx.Response(200, json=[{"updated_at": "2026-10-27T00:00:00Z"}])
        self.full_loads += 1
        offset = int(params["offset"])
        return httpx.Response(200, json=self.rows[offset:offset + int(params["limit"])])


class FakeLLM:
    def __init__(self, result=None, error=None):
        self.result = result or interp()
        self.error = error
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.result.model_dump_json()),
                                     finish_reason="stop")],
            usage=None,
        )


@pytest.fixture(autouse=True)
def facility_settings(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", SUPABASE_URL)
    monkeypatch.setattr(settings, "supabase_jwt_secret", JWT_SECRET)
    monkeypatch.setattr(settings, "supabase_anon_key", "anon")
    monkeypatch.setattr(settings, "consent_required", False)
    monkeypatch.setattr(settings, "openai_max_attempts", 1)
    facilities.reset()
    yield
    facilities.reset()
    facilities.set_http_client(None)
    openai_client.set_openai(None)


@pytest.fixture
def supabase():
    fake = FakeSupabase(ROWS)
    facilities.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    return fake


@pytest.fixture
def llm():
    fake = FakeLLM()
    openai_client.set_openai(fake)
    return fake


@pytest_asyncio.fixture
async def client(redis):
    set_redis(redis)
    app = FastAPI()
    v1.install(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    set_redis(None)


def body(**overrides):
    payload = {"profile": {"age": 36}, "symptom_tags": [], "location": {"lat": HOME[0], "lng": HOME[1]}}
    payload.update(overrides)
    return payload


async def suggest(client, **overrides):
    return await client.post("/v1/facility/suggest", json=body(**overrides), headers=auth())


@pytest.mark.asyncio
async def test_api_tags_only_does_not_call_llm(client, supabase, llm):
    resp = await suggest(client, symptom_tags=["headache", "dizziness"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert [f["id"] for f in data["facilities"]] == ["neuro-1", "gen-1"]
    assert data["interpretation"]["specialties"] == [
        {"key": "than_kinh", "label": "Nội thần kinh", "role": "primary", "from": ["headache", "dizziness"],
         "assumed": False, "reason": ""}
    ]
    assert data["data_version"] == "2026-10-27T00:00:00Z"
    assert llm.calls == []


@pytest.mark.asyncio
async def test_api_free_text_goes_through_llm_and_is_cached(client, supabase, llm):
    llm.result = interp(symptoms=[{"key": "headache", "matched_text": "nhức đầu"}])
    first = await suggest(client, text="nhức đầu 3 hôm nay")
    second = await suggest(client, text="nhức đầu 3 hôm nay", radius_km=25)
    assert first.json()["interpretation"]["symptoms"] == [
        {"key": "headache", "name": "Đau đầu", "source": "text", "matched_text": "nhức đầu"}
    ]
    assert second.json()["status"] == "ok"
    assert len(llm.calls) == 1
    sent = llm.calls[0]["messages"]
    assert "<symptom_text>" in sent[1]["content"]
    assert "headache: Đau đầu" in sent[0]["content"]


@pytest.mark.asyncio
async def test_api_emergency_keywords_work_even_when_llm_fails(client, supabase, llm):
    llm.error = RuntimeError("openai down")
    resp = await suggest(client, text="mẹ tôi méo miệng, yếu nửa người")
    data = resp.json()
    assert data["status"] == "emergency"
    assert data["emergency"]["kind"] == "stroke"
    assert data["emergency"]["call"] == "115"
    assert all(f["match"] == "general" for f in data["facilities"])


@pytest.mark.asyncio
async def test_api_llm_failure_falls_back_to_tags(client, supabase, llm):
    llm.error = RuntimeError("openai down")
    data = (await suggest(client, symptom_tags=["cough"], text="ho nhiều")).json()
    assert data["status"] == "ok"
    assert "chưa đọc được" in data["interpretation"]["note"]


@pytest.mark.asyncio
async def test_api_ambiguous_neuro_asks_then_uses_choice(client, supabase, llm):
    llm.result = interp(ambiguous_neuro=True)
    data = (await suggest(client, text="tôi muốn khám thần kinh")).json()
    assert data["status"] == "clarify"
    assert [o["value"] for o in data["clarify"]["options"]] == ["than_kinh", "tam_than", "general_only"]

    data = (await suggest(client, text="tôi muốn khám thần kinh", clarify_choice="tam_than")).json()
    assert data["status"] == "ok"
    assert data["facilities"][0]["id"] == TAM_THAN_HCM
    assert len(llm.calls) == 1  # lượt trả lời dùng kết quả đã cache


@pytest.mark.asyncio
async def test_api_unmapped_text_gives_general_with_note(client, supabase, llm):
    llm.result = interp(unmapped=["đau gót chân"])
    data = (await suggest(client, text="đau gót chân quá")).json()
    assert data["status"] == "ok"
    assert data["interpretation"]["unmapped_text"] == ["đau gót chân"]
    assert "đa khoa" in data["interpretation"]["note"]
    assert {f["match"] for f in data["facilities"]} == {"general"}


@pytest.mark.asyncio
async def test_api_empty_suggests_wider_radius(client, supabase, llm):
    data = (await suggest(client, symptom_tags=["rash"], location={"lat": 21.03, "lng": 105.85})).json()
    assert data["status"] == "empty"
    assert data["facilities"] == []
    assert data["empty"]["suggestions"] == [{"kind": "widen_radius", "radius_km": 25.0, "label": "Tìm trong 25 km"}]
    assert "Da liễu hoặc đa khoa" in data["empty"]["message"]


@pytest.mark.asyncio
async def test_api_rejects_bad_input(client, supabase, llm):
    assert (await suggest(client, symptom_tags=["not_a_tag"])).status_code == 422
    assert (await suggest(client)).status_code == 422
    assert (await suggest(client, symptom_tags=["cough"], clarify_choice="x")).status_code == 422
    assert (await suggest(client, text="a" * 501)).status_code == 422
    assert (await client.post("/v1/facility/suggest", json=body(symptom_tags=["cough"]))).status_code == 401


@pytest.mark.asyncio
async def test_api_daily_limit_counts_only_llm_calls(client, supabase, llm, monkeypatch):
    monkeypatch.setattr(settings, "facility_max_per_day", 2)
    llm.result = interp(symptoms=[{"key": "cough", "matched_text": "ho"}])
    # Chỉ thẻ, và dùng lại câu đã đọc (nới bán kính): không tính
    for _ in range(3):
        assert (await suggest(client, symptom_tags=["cough"])).status_code == 200
    assert (await suggest(client, text="ho nhiều")).status_code == 200
    assert (await suggest(client, text="ho nhiều", radius_km=25)).status_code == 200
    assert (await suggest(client, text="ho khan")).status_code == 200
    resp = await suggest(client, text="ho có đờm")
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_api_failed_llm_call_is_refunded(client, supabase, llm, monkeypatch, redis):
    monkeypatch.setattr(settings, "facility_max_per_day", 1)
    llm.error = RuntimeError("openai down")
    assert (await suggest(client, symptom_tags=["cough"], text="ho nhiều")).status_code == 200
    llm.error = None
    assert (await suggest(client, symptom_tags=["cough"], text="ho khan")).status_code == 200


@pytest.mark.asyncio
async def test_api_llm_over_budget_falls_back(client, supabase, llm, monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "facility_llm_budget_seconds", 0.05)

    async def slow(**kwargs):
        await asyncio.sleep(1)

    llm.chat.completions.create = slow
    data = (await suggest(client, symptom_tags=["cough"], text="ho nhiều")).json()
    assert data["status"] == "ok"
    assert "chưa đọc được" in data["interpretation"]["note"]


@pytest.mark.asyncio
async def test_api_unknown_age_is_adult(client, supabase, llm):
    data = (await suggest(client, profile={"age": None}, symptom_tags=["headache"])).json()
    assert data["interpretation"]["age_group"] == "adult"
    assert NHI_DONG_2 not in [f["id"] for f in data["facilities"]]


@pytest.mark.asyncio
async def test_api_emergency_facilities_can_be_routed(client, supabase, llm):
    data = (await suggest(client, text="méo miệng, nói đớ")).json()
    for f in data["facilities"]:
        assert f["id"] and isinstance(f["lat"], float) and isinstance(f["lng"], float)


@pytest.mark.asyncio
async def test_api_clarify_choice_with_wider_radius(client, supabase, llm):
    llm.result = interp(ambiguous_neuro=True)
    data = (await suggest(client, text="khám thần kinh", clarify_choice="than_kinh", radius_km=50)).json()
    assert data["status"] == "ok"
    assert data["facilities"][0]["id"] == "neuro-1"


@pytest.mark.asyncio
async def test_api_supabase_down(client, supabase, llm):
    supabase.fail = True
    resp = await suggest(client, symptom_tags=["cough"])
    assert resp.status_code == 503
    assert resp.json()["error"] == "unavailable"


@pytest.mark.asyncio
async def test_facility_cache_survives_supabase_outage(supabase, monkeypatch):
    first = await facilities.get()
    assert supabase.full_loads == 1
    monkeypatch.setattr(settings, "facility_refresh_seconds", 0)
    supabase.fail = True
    assert (await facilities.get()).facilities == first.facilities
    supabase.fail = False
    await facilities.get()
    assert supabase.full_loads == 1  # updated_at không đổi → không kéo lại


@pytest.mark.asyncio
async def test_api_after_limit_tags_only_retry_still_works(client, supabase, llm, monkeypatch):
    """Luồng của app khi gặp 429 có câu gõ: nút "Tìm bằng thẻ đã chọn" gửi lại
    request KHÔNG có text. Lượt đó không gọi model nên không được 429."""
    monkeypatch.setattr(settings, "facility_max_per_day", 1)
    llm.result = interp(symptoms=[{"key": "cough", "matched_text": "ho"}])
    assert (await suggest(client, symptom_tags=["cough"], text="ho nhiều")).status_code == 200
    blocked = await suggest(client, symptom_tags=["cough"], text="ho khan")
    assert blocked.status_code == 429 and blocked.json()["error"] == "rate_limited"
    retry = await suggest(client, symptom_tags=["cough"])
    assert retry.status_code == 200
    assert retry.json()["status"] == "ok"
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_api_locale_en_accepted_answers_in_vietnamese(client, supabase, llm):
    resp = await suggest(client, symptom_tags=["headache"], locale="en")
    assert resp.status_code == 200
    assert resp.json()["interpretation"]["specialties"][0]["label"] == "Nội thần kinh"


# --- Triệu chứng bổ sung + đối chiếu nguyên chữ (KAN-115, 2026-11-06) ----------

def test_extra_symptoms_use_known_keys_and_do_not_clash_with_app_tags():
    known = set(catalog.specialties()) | {catalog.GENERAL}
    assert not set(catalog.extra_symptoms()) & set(catalog.symptom_tags())
    for item in catalog.extra_symptoms().values():
        for group in ("adult", "child"):
            entry = item["map"][group]
            assert entry["primary"], item["key"]
            assert set(entry["primary"]) | set(entry["secondary"]) <= known, item["key"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("dau da day", [("abdominal_pain", "dau da day")]),
        ("đau đầu gối khi leo cầu thang", [("joint_pain", "đau đầu gối")]),
        ("sổ mũi, rát họng", [("stuffy_nose", "sổ mũi"), ("sore_throat", "rát họng")]),
        ("Trầm cảm", [("mental", "Trầm cảm")]),
        ("đau răng mấy hôm", [("toothache", "đau răng")]),
        ("không sốt nhưng ho", [("cough", "ho")]),
        ("không sốt ho", []),
        ("hết ho rồi, giờ đau họng", [("sore_throat", "đau họng")]),
        # cụm một từ phải khớp cả dấu; câu không dấu thì để model đọc
        ("họ bảo tôi bị ho", [("cough", "ho")]),
        ("ho bao toi bi sot", []),
        ("mẹ ơi", []),
    ],
)
def test_literal_matches(text, expected):
    assert catalog.literal_matches(text) == expected


def test_extra_symptoms_map_to_their_specialty():
    assert list(catalog.map_symptoms(["toothache"], "adult").specialties) == ["rang_ham_mat"]
    assert list(catalog.map_symptoms(["menstrual"], "adult").specialties) == ["san"]
    injury = catalog.map_symptoms(["injury"], "adult").specialties
    assert injury["ngoai_chan_thuong"].role == "primary"
    assert injury["co_xuong_khop"].role == "secondary"


def test_verify_phrase_handled_by_mental_flag_is_not_unmapped():
    """Log production 2026-11-05: 'Trầm cảm' bị log facility_unmapped dù đã gợi
    ý đúng Tâm thần, và app hiện ghi chú 'chưa có trong bảng'."""
    raw = interp(mental_health=True, unmapped=["Trầm cảm"])
    result = verify(raw, "Trầm cảm")
    assert result.unmapped == []
    assert result.symptoms == [("mental", "Trầm cảm")]


def test_verify_synonym_put_in_unmapped_becomes_symptom():
    raw = interp(symptoms=[{"key": "sore_throat", "matched_text": "rát họng"}], unmapped=["sổ mũi"])
    result = verify(raw, "sổ mũi, rát họng")
    assert result.symptoms == [("sore_throat", "rát họng"), ("stuffy_nose", "sổ mũi")]
    assert result.unmapped == []


def test_verify_table_beats_model_when_grounding_is_a_listed_phrase():
    raw = interp(symptoms=[{"key": "headache", "matched_text": "dau da day"}])
    assert verify(raw, "dau da day").symptoms == [("abdominal_pain", "dau da day")]


def test_verify_keeps_model_key_when_grounding_is_a_paraphrase():
    raw = interp(symptoms=[{"key": "headache", "matched_text": "đầu như búa bổ"}])
    assert verify(raw, "đầu như búa bổ").symptoms == [("headache", "đầu như búa bổ")]


def test_verify_ambiguous_neuro_phrase_is_not_unmapped():
    raw = interp(ambiguous_neuro=True, unmapped=["khám thần kinh"])
    assert verify(raw, "tôi muốn khám thần kinh").unmapped == []


@pytest.mark.asyncio
async def test_api_mental_phrase_has_no_unmapped_note(client, supabase, llm):
    llm.result = interp(mental_health=True, unmapped=["Trầm cảm"])
    data = (await suggest(client, text="Trầm cảm")).json()
    assert data["interpretation"]["unmapped_text"] == []
    assert data["interpretation"]["note"] is None
    assert [s["key"] for s in data["interpretation"]["specialties"]] == ["tam_than"]
    names = [s["name"] for s in data["interpretation"]["symptoms"]]
    assert names == ["Vấn đề tâm lý"]


@pytest.mark.parametrize(
    "text, expected",
    [
        # bỏ dấu thì trùng nhau: câu có dấu khớp đúng, câu không dấu để model đọc
        ("hết đau bụng rồi, giờ bị tiêu chảy", [("diarrhea", "tiêu chảy")]),
        ("khó tiêu", [("bloating", "khó tiêu")]),
        ("khó tiểu", [("urinary", "khó tiểu")]),
        ("kho tieu", []),
        ("đỏ mắt", [("eye_pain", "đỏ mắt")]),
        ("do mat", []),
    ],
)
def test_literal_matches_accent_collisions(text, expected):
    assert catalog.literal_matches(text) == expected


# --- Giả định chuyên khoa cho cụm ngoài bảng (KAN-134) -----------------------

def _walk(schema: dict, defs: dict):
    if "$ref" in schema:
        yield from _walk(defs[schema["$ref"].split("/")[-1]], defs)
        return
    if schema.get("type") == "object":
        yield schema
        for sub in schema.get("properties", {}).values():
            yield from _walk(sub, defs)
    for key in ("anyOf", "allOf"):
        for sub in schema.get(key, []):
            yield from _walk(sub, defs)
    if "items" in schema:
        yield from _walk(schema["items"], defs)


def test_verify_keeps_allowed_guess_and_drops_excluded():
    # Enum của schema đã loại tam_than / nhi nên model không trả được; dựng đối tượng thô
    # để kiểm lớp phòng thủ thứ hai trong verify().
    raw = interp()
    raw = SimpleNamespace(**{**raw.__dict__, "unmapped": [
        SimpleNamespace(text="đau gót chân", specialty="co_xuong_khop", reason="đau gót chân khi đi"),
        SimpleNamespace(text="hay lo lắng", specialty="tam_than", reason="lo âu"),
        SimpleNamespace(text="bé hay quấy", specialty="nhi", reason="trẻ quấy"),
    ]})
    result = verify(raw, "đau gót chân, hay lo lắng, bé hay quấy")
    assert result.guesses == [("đau gót chân", "co_xuong_khop", "đau gót chân khi đi")]
    assert result.unmapped == ["đau gót chân", "hay lo lắng", "bé hay quấy"]


def test_guess_reason_is_truncated():
    raw = interp(unmapped=[{"text": "đau gót chân", "specialty": "co_xuong_khop", "reason": "x" * 200}])
    assert len(verify(raw, "đau gót chân").guesses[0][2]) == 80


def test_table_wins_over_guess():
    mapping = catalog.map_symptoms(["joint_pain"], "adult", guesses=[("đau gót", "co_xuong_khop", "r")])
    pick = mapping.specialties["co_xuong_khop"]
    assert (pick.role, pick.assumed) == ("primary", False)


def test_guess_becomes_assumed_secondary():
    mapping = catalog.map_symptoms([], "adult", guesses=[("nhìn đôi", "mat", "nhìn một thành hai")])
    pick = mapping.specialties["mat"]
    assert (pick.role, pick.assumed, pick.reason, pick.sources) == ("secondary", True, "nhìn một thành hai", ["assumed"])


def test_assumed_specialty_ranks_level_with_general():
    general_far = row("g", "Đa khoa xa", is_general=True, lat=HOME[0] + 0.05)
    eye_near = row("m", "BV Mắt", specialties=["mat"], lat=HOME[0] + 0.01)
    general_near = row("g2", "Đa khoa gần", is_general=True, lat=HOME[0] + 0.001)
    mapping = catalog.map_symptoms([], "adult", guesses=[("nhìn đôi", "mat", "nhìn một thành hai")])
    ranked = ranking.rank(snapshot_of([general_far, eye_near, general_near]), mapping, "adult", *HOME, 50, 10)
    assert [r.facility.id for r in ranked] == ["g2", "m", "g"]
    eye = ranked[1]
    assert eye.match == "secondary"
    assert eye.reason.startswith("Bụt giả định: nhìn một thành hai → Mắt")


@pytest.mark.asyncio
async def test_api_assumed_specialty_is_labelled(client, supabase, llm):
    llm.result = interp(unmapped=[{"text": "nhìn đôi", "specialty": "mat", "reason": "nhìn một thành hai"}])
    data = (await suggest(client, text="mấy hôm nay nhìn đôi")).json()
    [spec] = data["interpretation"]["specialties"]
    assert spec == {"key": "mat", "label": "Mắt", "role": "secondary", "from": ["assumed"], "assumed": True,
                    "reason": "nhìn một thành hai"}
    assert "Bụt tạm đoán" in data["interpretation"]["note"]


@pytest.mark.asyncio
async def test_api_table_specialty_has_assumed_false(client, supabase, llm):
    data = (await suggest(client, symptom_tags=["headache"], text=None)).json()
    assert all(s["assumed"] is False for s in data["interpretation"]["specialties"])


@pytest.mark.asyncio
async def test_unmapped_log_includes_guess(client, supabase, llm, caplog):
    llm.result = interp(unmapped=[
        {"text": "nhìn đôi", "specialty": "mat", "reason": "nhìn một thành hai"},
        {"text": "đau gót chân", "specialty": "", "reason": ""},
    ])
    caplog.set_level(logging.INFO)
    await suggest(client, text="mấy hôm nay nhìn đôi, đau gót chân")
    lines = [r.getMessage() for r in caplog.records if "facility_unmapped" in r.getMessage()]
    assert any("guess=mat" in line and "nhìn đôi" in line for line in lines)
    assert any("guess=none" in line and "đau gót chân" in line for line in lines)


def test_schema_is_strict_and_guess_enum_excludes_mental_and_pediatric():
    schema = SymptomInterpretation.model_json_schema()
    defs = schema.get("$defs", {})
    for obj in _walk(schema, defs):
        assert obj.get("additionalProperties") is False
        assert set(obj["required"]) == set(obj["properties"])
    enum_values = None
    for obj in _walk(schema, defs):
        if "specialty" in obj.get("properties", {}):
            sub = obj["properties"]["specialty"]
            enum_values = defs[sub["$ref"].split("/")[-1]]["enum"] if "$ref" in sub else sub["enum"]
    assert enum_values is not None
    assert "" in enum_values
    assert "tam_than" not in enum_values and "nhi" not in enum_values
    assert "mat" in enum_values
