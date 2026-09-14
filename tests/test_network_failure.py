"""
Kịch bản Phase 6: "ngắt kết nối client giữa chừng, gửi lại, verify không mất /
không lặp câu trả lời."

Ghép các mảnh thật lại với nhau (API -> consumer -> bộ đệm replay -> SSE), chỉ
thay Kafka và OpenAI bằng bản giả. Kafka thật được kiểm bằng scripts/loadtest.py.
"""

import asyncio
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.chat import router
from app.auth import issue_token
from app.kafka import consumer
from app.redis_client import set_redis
from app.services import openai_client, quota
from app.sse import hub, replay
from app.sse.stream import event_stream
from tests.fake_openai import FakeOpenAI
from tests.test_sse import StubRequest

USER_ID = 300


@pytest_asyncio.fixture
async def wired(redis, monkeypatch):
    """Nối API -> consumer -> bộ đệm SSE, thay đúng 2 biên: Kafka và OpenAI."""
    set_redis(redis)
    hub.reset()

    enqueued: list = []
    fake_openai = FakeOpenAI()
    openai_client.set_openai(fake_openai)

    async def fake_enqueue(message):
        enqueued.append(message)

    async def fake_publish_response(response):
        # Đây chính là việc response_consumer làm khi nhận message từ Kafka
        await replay.remember(redis, response)
        hub.publish(response)

    monkeypatch.setattr("app.api.chat.publish_chat_request", fake_enqueue)
    monkeypatch.setattr(consumer, "publish_chat_response", fake_publish_response)

    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, enqueued, fake_openai

    openai_client.set_openai(None)
    set_redis(None)
    hub.reset()


def headers() -> dict:
    return {"Authorization": f"Bearer {issue_token(USER_ID, 'free')}"}


async def test_mat_mang_giua_chung_roi_gui_lai_khong_mat_khong_lap(wired, redis):
    client, enqueued, fake_openai = wired
    request_id = str(uuid4())
    body = {"request_id": request_id, "content": "Tôi bị đau đầu 3 ngày"}

    # 1. Client gửi câu hỏi rồi rớt mạng ngay (không kịp mở SSE)
    first = await client.post("/chat/ask", json=body, headers=headers())
    assert first.status_code == 202

    # 2. Consumer vẫn xử lý xong trong lúc client offline
    await consumer._handle(enqueued[0])

    # 3. Client online lại và gửi lại y hệt request cũ (không biết đã xong)
    retry = await client.post("/chat/ask", json=body, headers=headers())
    assert retry.status_code == 200
    assert retry.json()["status"] == "done", "câu trả lời đã có mà không trả lại được"

    # KHÔNG LẶP: đúng 1 lần gọi OpenAI, 1 lần enqueue, 1 lượt quota
    assert len(fake_openai.calls) == 1
    assert len(enqueued) == 1
    assert await quota.remaining(redis, USER_ID, "free") == 1

    # KHÔNG MẤT: mở lại SSE thì vẫn nhận được câu trả lời đã phát lúc offline
    frames = [f async for f in event_stream(StubRequest(0), USER_ID, None)]
    assert frames == []  # chưa gửi last_request_id thì không phát lại

    replayed = [
        f async for f in event_stream(StubRequest(0), USER_ID, uuid4())
    ]  # mốc lạ -> phát lại toàn bộ
    assert len(replayed) == 1
    assert request_id in replayed[0]


async def test_reconnect_giua_hai_cau_hoi_chi_nhan_phan_con_thieu(wired, redis, monkeypatch):
    monkeypatch.setattr("app.config.settings.quota_free_per_day", 5, raising=False)
    client, enqueued, _ = wired

    ids = []
    for i in range(3):
        rid = str(uuid4())
        ids.append(rid)
        await client.post(
            "/chat/ask",
            json={"request_id": rid, "content": f"câu hỏi {i}"},
            headers=headers(),
        )
        await consumer._handle(enqueued[-1])

    # Client đã nhận tới câu 1, rớt mạng, reconnect
    missed = [f async for f in event_stream(StubRequest(0), USER_ID, ids[0])]
    assert len(missed) == 2
    assert ids[1] in missed[0] and ids[2] in missed[1]


async def test_gui_lai_nhieu_lan_lien_tuc_van_chi_ton_1_luot(wired, redis):
    """Mạng chập chờn thật thì client retry liên tục, không phải đúng 1 lần."""
    client, enqueued, fake_openai = wired
    body = {"request_id": str(uuid4()), "content": "đau đầu"}

    responses = await asyncio.gather(
        *(client.post("/chat/ask", json=body, headers=headers()) for _ in range(8))
    )
    accepted = [r for r in responses if r.json()["status"] == "accepted"]

    assert len(accepted) == 1
    assert len(enqueued) == 1
    assert await quota.remaining(redis, USER_ID, "free") == 1


async def test_nhieu_user_dong_thoi_khong_lan_quota_va_khong_lan_cau_tra_loi(wired, redis):
    client, enqueued, _ = wired

    async def ask(user_id: int):
        return await client.post(
            "/chat/ask",
            json={"content": f"câu hỏi của user {user_id}"},
            headers={"Authorization": f"Bearer {issue_token(user_id, 'free')}"},
        )

    user_ids = list(range(400, 430))
    await asyncio.gather(*(ask(uid) for uid in user_ids))

    assert len(enqueued) == len(user_ids)
    # mỗi user bị trừ đúng 1 lượt, không ai bị trừ nhầm quota của người khác
    for uid in user_ids:
        assert await quota.remaining(redis, uid, "free") == 1

    for message in enqueued:
        assert str(message.user_id) in message.content
