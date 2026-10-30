"""Gợi ý cơ sở khám theo triệu chứng (KAN-115)."""

import json
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
    mapping = catalog.map_symptoms(["fever"], "adult")
    assert mapping.specialties == {}
    assert mapping.general_symptoms == ["fever"]


def test_primary_wins_over_secondary():
    # abdominal_pain: tieu_hoa chính, ngoai_tong_quat phụ
    mapping = catalog.map_symptoms(["abdominal_pain"], "adult")
    assert mapping.specialties["tieu_hoa"].role == "primary"
    assert mapping.specialties["ngoai_tong_quat"].role == "secondary"


def test_age_group_boundary():
    assert catalog.age_group(16) == "child"
    assert catalog.age_group(17) == "adult"


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


def test_radius_and_limit():
    mapping = catalog.map_symptoms(["fever"], "adult")
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
    return SymptomInterpretation.model_validate(base)


def test_verify_drops_symptoms_without_grounding():
    raw = interp(
        symptoms=[{"key": "headache", "matched_text": "nhức đầu"}, {"key": "fever", "matched_text": "sốt cao"}],
        unmapped=["đau răng", "bịa thêm"],
    )
    result = verify(raw, "Nhuc dau va dau rang")
    assert result.symptoms == [("headache", "nhức đầu")]
    assert result.unmapped == ["đau răng"]


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
        {"key": "than_kinh", "label": "Nội thần kinh", "role": "primary", "from": ["headache", "dizziness"]}
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
    llm.result = interp(unmapped=["đau răng"])
    data = (await suggest(client, text="đau răng quá")).json()
    assert data["status"] == "ok"
    assert data["interpretation"]["unmapped_text"] == ["đau răng"]
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
async def test_api_daily_limit(client, supabase, llm, monkeypatch):
    monkeypatch.setattr(settings, "facility_max_per_day", 2)
    for _ in range(2):
        assert (await suggest(client, symptom_tags=["cough"])).status_code == 200
    resp = await suggest(client, symptom_tags=["cough"])
    assert resp.status_code == 429
    assert resp.json()["error"] == "rate_limited"


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
