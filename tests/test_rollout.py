import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.chat import router
from app.auth import issue_token
from app.config import settings
from app.redis_client import set_redis
from app.services import rollout


def test_tat_han_thi_khong_ai_duoc_bat(monkeypatch):
    monkeypatch.setattr(settings, "rollout_enabled", False)
    monkeypatch.setattr(settings, "rollout_percentage", 100)
    monkeypatch.setattr(settings, "rollout_allowlist", "1,2,3")
    assert not rollout.is_enabled(1)


def test_allowlist_luon_duoc_bat_du_phan_tram_bang_0(monkeypatch):
    monkeypatch.setattr(settings, "rollout_percentage", 0)
    monkeypatch.setattr(settings, "rollout_allowlist", "10, 20 ,30")
    assert rollout.is_enabled(10) and rollout.is_enabled(30)
    assert not rollout.is_enabled(11)


def test_ket_qua_on_dinh_giua_cac_lan_goi(monkeypatch):
    """Không ổn định thì cùng một user lúc hỏi được lúc không — không thể theo
    dõi được nhóm rollout, mà user thì thấy hệ thống lúc có lúc không."""
    monkeypatch.setattr(settings, "rollout_percentage", 50)
    for user_id in range(200):
        assert len({rollout.is_enabled(user_id) for _ in range(5)}) == 1


def test_phan_tram_chia_xap_xi_dung(monkeypatch):
    monkeypatch.setattr(settings, "rollout_percentage", 20)
    enabled = sum(rollout.is_enabled(uid) for uid in range(5000))
    assert 850 < enabled < 1150  # ~20% với dung sai


def test_khong_dung_chia_du_vi_user_id_cap_tuan_tu(monkeypatch):
    """user_id tuần tự + `% 100` sẽ gom user đăng ký cùng đợt vào cùng nhóm,
    mẫu rollout mất tính đại diện."""
    monkeypatch.setattr(settings, "rollout_percentage", 10)
    consecutive = [rollout.is_enabled(uid) for uid in range(1000, 1100)]
    assert 0 < sum(consecutive) < 100  # không phải tất cả, cũng không phải không ai


def test_0_phan_tram_khong_bat_cho_ai(monkeypatch):
    monkeypatch.setattr(settings, "rollout_percentage", 0)
    assert not any(rollout.is_enabled(uid) for uid in range(1000))


def test_100_phan_tram_bat_cho_tat_ca(monkeypatch):
    monkeypatch.setattr(settings, "rollout_percentage", 100)
    assert all(rollout.is_enabled(uid) for uid in range(1000))


@pytest_asyncio.fixture
async def client(redis):
    set_redis(redis)
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    set_redis(None)


async def test_user_ngoai_nhom_rollout_bi_tu_choi_ro_rang(client, monkeypatch):
    monkeypatch.setattr(settings, "rollout_percentage", 0)
    resp = await client.post(
        "/chat/ask",
        json={"content": "đau đầu"},
        headers={"Authorization": f"Bearer {issue_token(777, 'free')}"},
    )
    assert resp.status_code == 403
    assert "chưa được mở" in resp.json()["detail"]


async def test_bi_chan_boi_rollout_thi_khong_ton_quota(client, redis, monkeypatch):
    monkeypatch.setattr(settings, "rollout_percentage", 0)
    headers = {"Authorization": f"Bearer {issue_token(778, 'free')}"}
    await client.post("/chat/ask", json={"content": "đau đầu"}, headers=headers)

    from app.services import quota

    assert await quota.remaining(redis, 778, "free") == 2
