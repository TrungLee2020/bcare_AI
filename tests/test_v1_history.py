from uuid import uuid4

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import v1
from app.config import settings
from app.db import session as db_session_module
from app.redis_client import set_redis
from app.schemas import ChatRequestMessage, ChatResponseMessage
from app.services import history
from tests.fake_openai import make_answer
from tests.test_v1_api import JWT_SECRET, SUPABASE_URL, USER, auth

OTHER = "1b2c3d4e-5555-4666-8777-988899990000"


@pytest_asyncio.fixture
async def client(redis, db_maker, monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", SUPABASE_URL)
    monkeypatch.setattr(settings, "supabase_jwt_secret", JWT_SECRET)
    set_redis(redis)
    app = FastAPI()
    v1.install(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    set_redis(None)


async def turn(session_id, question, answer="Bạn nên nghỉ ngơi và theo dõi thêm.", user_id=USER, **answer_fields):
    message = ChatRequestMessage(
        request_id=uuid4(), user_id=user_id, session_id=session_id, tier="free", content=question,
    )
    await history.record_turn(
        message,
        ChatResponseMessage(
            request_id=message.request_id, user_id=user_id, session_id=session_id, status="ok",
            answer=make_answer(answer=answer, **answer_fields),
            prompt_version=settings.prompt_version, model="fake",
        ),
    )


# --- GET /v1/conversations --------------------------------------------------

async def test_danh_sach_hoi_thoai_moi_hoat_dong_truoc(client):
    a, b = uuid4(), uuid4()
    await turn(a, "Tôi bị đau đầu 3 ngày")
    await turn(b, "Tôi bị mất ngủ")
    await turn(a, "Có cần đi khám không?", answer="Bạn nên đi khám nếu kéo dài.")

    resp = await client.get("/v1/conversations", headers=auth())
    assert resp.status_code == 200
    data = resp.json()
    assert [c["conversation_id"] for c in data["conversations"]] == [str(a), str(b)]
    first = data["conversations"][0]
    assert first["title"] == "Tôi bị đau đầu 3 ngày"
    assert first["preview"] == "Bạn nên đi khám nếu kéo dài."
    assert first["message_count"] == 4
    assert first["created_at"] and first["updated_at"]
    assert data["next_cursor"] is None


async def test_khong_thay_hoi_thoai_cua_nguoi_khac(client):
    await turn(uuid4(), "câu của người khác", user_id=OTHER)
    resp = await client.get("/v1/conversations", headers=auth())
    assert resp.json() == {"conversations": [], "next_cursor": None}


async def test_phan_trang_danh_sach_hoi_thoai(client):
    ids = [uuid4() for _ in range(5)]
    for i, sid in enumerate(ids):
        await turn(sid, f"câu {i}")

    seen, cursor = [], None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        data = (await client.get("/v1/conversations", params=params, headers=auth())).json()
        assert len(data["conversations"]) <= 2
        seen += [c["conversation_id"] for c in data["conversations"]]
        cursor = data["next_cursor"]
        if cursor is None:
            break
    assert seen == [str(sid) for sid in reversed(ids)]


async def test_tieu_de_dai_bi_cat_va_gop_xuong_dong(client):
    await turn(uuid4(), "dòng một\n\n" + "a" * 200)
    title = (await client.get("/v1/conversations", headers=auth())).json()["conversations"][0]["title"]
    assert "\n" not in title
    assert title.endswith("…")
    assert len(title) <= v1.TITLE_CHARS + 1


async def test_cursor_sai_tra_422(client):
    resp = await client.get("/v1/conversations", params={"cursor": "abc"}, headers=auth())
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_request"


async def test_khong_co_token_tra_401(client):
    resp = await client.get("/v1/conversations")
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


async def test_loi_db_tra_503(client, monkeypatch):
    monkeypatch.setattr(db_session_module, "_sessionmaker", None)
    resp = await client.get("/v1/conversations", headers=auth())
    assert resp.status_code == 503
    assert resp.json()["error"] == "unavailable"


# --- GET /v1/conversations/{id}/messages ------------------------------------

async def test_tin_nhan_theo_thu_tu_cu_den_moi(client):
    sid = uuid4()
    await turn(sid, "Tôi bị đau đầu", should_see_doctor=False, follow_up_questions=["Bạn có sốt không?"])
    await turn(sid, "Đau cả đêm", answer="Bạn nên đi khám sớm.", should_see_doctor=True)

    resp = await client.get(f"/v1/conversations/{sid}/messages", headers=auth())
    assert resp.status_code == 200
    data = resp.json()
    assert data["conversation_id"] == str(sid)
    assert [(m["role"], m["content"]) for m in data["messages"]] == [
        ("user", "Tôi bị đau đầu"),
        ("assistant", "Bạn nên nghỉ ngơi và theo dõi thêm."),
        ("user", "Đau cả đêm"),
        ("assistant", "Bạn nên đi khám sớm."),
    ]
    assert data["messages"][1]["follow_up_questions"] == ["Bạn có sốt không?"]
    assert data["messages"][3]["should_see_doctor"] is True
    assert "should_see_doctor" not in data["messages"][0]
    assert data["next_cursor"] is None


async def test_phan_trang_tin_nhan_ve_phia_cu_hon(client):
    sid = uuid4()
    for i in range(3):
        await turn(sid, f"câu {i}", answer=f"đáp {i}")

    page1 = (await client.get(f"/v1/conversations/{sid}/messages", params={"limit": 4}, headers=auth())).json()
    assert [m["content"] for m in page1["messages"]] == ["câu 1", "đáp 1", "câu 2", "đáp 2"]
    page2 = (await client.get(
        f"/v1/conversations/{sid}/messages", params={"limit": 4, "cursor": page1["next_cursor"]}, headers=auth(),
    )).json()
    assert [m["content"] for m in page2["messages"]] == ["câu 0", "đáp 0"]
    assert page2["next_cursor"] is None


async def test_hoi_thoai_cua_nguoi_khac_tra_404(client):
    sid = uuid4()
    await turn(sid, "câu riêng tư", user_id=OTHER)
    resp = await client.get(f"/v1/conversations/{sid}/messages", headers=auth())
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


async def test_hoi_thoai_khong_ton_tai_hoac_id_sai_tra_404(client):
    for cid in (str(uuid4()), "khong-phai-uuid"):
        resp = await client.get(f"/v1/conversations/{cid}/messages", headers=auth())
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"


# --- DELETE /v1/conversations[/{id}] ---------------------------------------

async def test_xoa_mot_hoi_thoai_an_ngay_khoi_lich_su(client):
    keep, gone = uuid4(), uuid4()
    await turn(keep, "giữ lại")
    await turn(gone, "xoá đi")

    resp = await client.delete(f"/v1/conversations/{gone}", headers=auth())
    assert resp.status_code == 204

    listed = (await client.get("/v1/conversations", headers=auth())).json()["conversations"]
    assert [c["conversation_id"] for c in listed] == [str(keep)]
    resp = await client.get(f"/v1/conversations/{gone}/messages", headers=auth())
    assert resp.status_code == 404


async def test_xoa_hoi_thoai_khong_ton_tai_da_xoa_hoac_cua_nguoi_khac_tra_404(client):
    mine, theirs = uuid4(), uuid4()
    await turn(mine, "của tôi")
    await turn(theirs, "của người khác", user_id=OTHER)
    await client.delete(f"/v1/conversations/{mine}", headers=auth())

    for cid in (str(uuid4()), "khong-phai-uuid", str(mine), str(theirs)):
        resp = await client.delete(f"/v1/conversations/{cid}", headers=auth())
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"
    # Hội thoại của người khác vẫn còn nguyên với chủ của nó
    resp = await client.get(f"/v1/conversations/{theirs}/messages", headers=auth(sub=OTHER))
    assert resp.status_code == 200


async def test_xoa_toan_bo_chi_xoa_cua_minh(client):
    await turn(uuid4(), "câu 1")
    await turn(uuid4(), "câu 2")
    await turn(uuid4(), "của người khác", user_id=OTHER)

    resp = await client.delete("/v1/conversations", headers=auth())
    assert resp.status_code == 204
    assert (await client.get("/v1/conversations", headers=auth())).json()["conversations"] == []
    others = (await client.get("/v1/conversations", headers=auth(sub=OTHER))).json()["conversations"]
    assert len(others) == 1
    # Gọi lại khi đã hết thì vẫn 204
    assert (await client.delete("/v1/conversations", headers=auth())).status_code == 204


async def test_xoa_khong_co_token_tra_401(client):
    assert (await client.delete("/v1/conversations")).status_code == 401
    assert (await client.delete(f"/v1/conversations/{uuid4()}")).status_code == 401
