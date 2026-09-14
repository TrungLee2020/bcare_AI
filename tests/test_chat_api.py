import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.chat import router
from app.auth import issue_token
from app.redis_client import set_redis
from app.services import idempotency


class FakeProducer:
    """Thay cho Kafka thật: ghi lại các message đã publish để assert."""

    def __init__(self):
        self.published = []
        self.fail = False

    async def __call__(self, message):
        if self.fail:
            raise RuntimeError("kafka down")
        self.published.append(message)


@pytest.fixture
def producer(monkeypatch):
    fake = FakeProducer()
    monkeypatch.setattr("app.api.chat.publish_chat_request", fake)
    return fake


@pytest_asyncio.fixture
async def client(redis):
    set_redis(redis)
    app = FastAPI()
    app.include_router(router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    set_redis(None)


def body(**overrides):
    payload = {"content": "Tôi bị đau đầu 3 ngày"}
    payload.update(overrides)
    return payload


def auth(user_id: int = 100, tier: str = "free") -> dict:
    """user_id/tier đến từ token, không phải từ body."""
    return {"Authorization": f"Bearer {issue_token(user_id, tier)}"}


async def test_request_dau_tien_duoc_enqueue(client, producer):
    resp = await client.post("/chat/ask", json=body(), headers=auth())
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "accepted"
    assert data["quota_remaining"] == 1
    assert len(producer.published) == 1


async def test_retry_cung_request_id_chi_ton_1_quota_va_1_lan_enqueue(client, producer):
    """Giả lập client mất mạng và gửi lại 5 lần cùng request_id."""
    rid = str(uuid4())
    statuses = []
    for _ in range(5):
        resp = await client.post("/chat/ask", json=body(request_id=rid), headers=auth())
        statuses.append(resp.json()["status"])

    assert statuses == ["accepted", "duplicate", "duplicate", "duplicate", "duplicate"]
    assert len(producer.published) == 1
    # chỉ tốn đúng 1 lượt, user free vẫn còn 1 câu hỏi
    assert (await client.post("/chat/ask", json=body(), headers=auth())).json()["quota_remaining"] == 0


async def test_retry_dong_thoi_cung_request_id_chi_qua_1_cai(client, producer):
    rid = str(uuid4())
    responses = await asyncio.gather(
        *(client.post("/chat/ask", json=body(request_id=rid), headers=auth()) for _ in range(10))
    )
    accepted = [r for r in responses if r.json()["status"] == "accepted"]
    assert len(accepted) == 1
    assert len(producer.published) == 1


async def test_duplicate_tra_lai_cau_tra_loi_da_cache(client, producer, redis):
    rid = str(uuid4())
    await client.post("/chat/ask", json=body(request_id=rid), headers=auth())
    await idempotency.save_response(redis, rid, {"answer": "Bạn nên nghỉ ngơi..."})

    resp = await client.post("/chat/ask", json=body(request_id=rid), headers=auth())
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "done"
    assert data["answer"]["answer"] == "Bạn nên nghỉ ngơi..."
    assert len(producer.published) == 1


async def test_het_quota_tra_429_va_khong_enqueue(client, producer):
    for _ in range(2):
        await client.post("/chat/ask", json=body(), headers=auth())

    resp = await client.post("/chat/ask", json=body(), headers=auth())
    assert resp.status_code == 429
    assert "hết lượt hỏi hôm nay" in resp.json()["detail"]
    assert len(producer.published) == 2


async def test_het_quota_van_nha_request_id_de_hom_sau_gui_lai_duoc(
    client, producer, redis
):
    rid = str(uuid4())
    for _ in range(2):
        await client.post("/chat/ask", json=body(), headers=auth())
    assert (await client.post("/chat/ask", json=body(request_id=rid), headers=auth())).status_code == 429

    # giả lập sang ngày mới: quota counter hết hạn
    await redis.flushdb()

    resp = await client.post("/chat/ask", json=body(request_id=rid), headers=auth())
    assert resp.status_code == 202, "request_id bị 'cháy' vì hết quota -> không bao giờ hỏi được"
    assert resp.json()["status"] == "accepted"


async def test_enqueue_loi_thi_hoan_quota_va_cho_retry(client, producer):
    producer.fail = True
    rid = str(uuid4())
    resp = await client.post("/chat/ask", json=body(request_id=rid), headers=auth())
    assert resp.status_code == 503

    producer.fail = False
    retry = await client.post("/chat/ask", json=body(request_id=rid), headers=auth())
    assert retry.status_code == 202
    # lần lỗi không được tính quota -> vẫn còn 1 lượt
    assert retry.json()["quota_remaining"] == 1


async def test_content_rong_bi_tu_choi(client, producer):
    resp = await client.post("/chat/ask", json=body(content=""), headers=auth())
    assert resp.status_code == 422
    assert not producer.published
