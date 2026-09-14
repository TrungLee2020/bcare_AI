import time

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.chat import router
from app.auth import AuthError, Principal, issue_token, verify_token
from app.config import settings
from app.redis_client import set_redis


def test_token_hop_le_tra_ve_dung_danh_tinh():
    assert verify_token(issue_token(7, "premium")) == Principal(7, "premium")


def test_token_bi_sua_noi_dung_thi_khong_qua():
    """Đổi user_id trong payload mà không có secret thì chữ ký không khớp."""
    token = issue_token(7, "free")
    payload, signature = token.split(".", 1)
    forged = issue_token(999, "free").split(".", 1)[0]
    with pytest.raises(AuthError):
        verify_token(f"{forged}.{signature}")


def test_token_het_han_bi_tu_choi():
    with pytest.raises(AuthError, match="hết hạn"):
        verify_token(issue_token(1, "free", ttl_seconds=-1))


def test_token_ky_bang_secret_khac_bi_tu_choi(monkeypatch):
    token = issue_token(1, "premium")
    monkeypatch.setattr(settings, "auth_secret", "secret-khac")
    with pytest.raises(AuthError):
        verify_token(token)


def test_tier_bia_dat_bi_tu_choi():
    import base64, hmac, json
    from hashlib import sha256

    payload = json.dumps(
        {"user_id": 1, "tier": "vip", "exp": 9999999999}, separators=(",", ":")
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = base64.urlsafe_b64encode(
        hmac.new(settings.auth_secret.encode(), payload, sha256).digest()
    ).decode().rstrip("=")

    with pytest.raises(AuthError, match="Tier"):
        verify_token(f"{encoded}.{signature}")


def test_token_rac_khong_lam_no_server():
    for rubbish in ("", "abc", "a.b.c", "...", "eyJ4Ijox"):
        with pytest.raises(AuthError):
            verify_token(rubbish)


# --- qua HTTP ----------------------------------------------------------

@pytest_asyncio.fixture
async def client(redis):
    set_redis(redis)
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    set_redis(None)


async def test_khong_co_token_thi_401(client):
    resp = await client.post("/chat/ask", json={"content": "đau đầu"})
    assert resp.status_code == 401


async def test_tier_trong_body_khong_nang_duoc_quota(client, monkeypatch):
    """Lỗ hổng trước Phase 6: client tự khai tier premium để được 5 câu/ngày.
    Giờ tier chỉ đến từ token, body có gửi gì cũng bị bỏ qua."""
    monkeypatch.setattr(
        "app.api.chat.publish_chat_request", lambda *a, **k: _noop()
    )
    headers = {"Authorization": f"Bearer {issue_token(500, 'free')}"}
    body = {"content": "đau đầu", "tier": "premium", "user_id": 999}

    first = await client.post("/chat/ask", json=body, headers=headers)
    assert first.status_code == 202
    # vẫn là hạn mức free (2 câu), không phải premium (5 câu)
    assert first.json()["quota_remaining"] == 1


async def _noop():
    return None


async def test_user_id_trong_body_khong_gia_mao_duoc_nguoi_khac(client, monkeypatch):
    monkeypatch.setattr("app.api.chat.publish_chat_request", lambda *a, **k: _noop())
    published = []

    async def capture(message):
        published.append(message)

    monkeypatch.setattr("app.api.chat.publish_chat_request", capture)
    headers = {"Authorization": f"Bearer {issue_token(501, 'free')}"}

    await client.post(
        "/chat/ask", json={"content": "đau đầu", "user_id": 42}, headers=headers
    )
    assert published[0].user_id == 501


def test_mac_dinh_phai_bat_auth():
    """Mặc định phải là fail-closed: quên cấu hình thì kín, không phải hở."""
    from app.config import Settings

    assert Settings(_env_file=None).auth_required is True
