"""
Kafka giao lại message khi consumer chết trước lúc commit offset. Dấu "đã xử
lý" là câu trả lời đã cache — không phải một cờ "đang xử lý" đặt từ đầu, vì cờ
kiểu đó làm mất luôn message của instance bị kill giữa chừng.
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kafka import consumer
from app.redis_client import set_redis
from app.schemas import ChatRequestMessage
from app.services import idempotency, openai_client
from app.sse import replay
from tests.fake_openai import FakeOpenAI


def record(message: ChatRequestMessage, offset: int = 0):
    return SimpleNamespace(
        value=message.model_dump_json().encode(),
        topic="chat_requests",
        partition=0,
        offset=offset,
        key=b"1",
    )


def question() -> ChatRequestMessage:
    return ChatRequestMessage(user_id=1, tier="free", content="Tôi bị đau đầu")


@pytest.fixture
def wired(redis, monkeypatch):
    published = []

    async def fake_publish(message):
        published.append(message)

    monkeypatch.setattr(consumer, "publish_chat_response", fake_publish)
    fake = FakeOpenAI()
    set_redis(redis)
    openai_client.set_openai(fake)
    yield published, fake
    openai_client.set_openai(None)
    set_redis(None)


async def test_giao_lai_message_da_tra_loi_thi_khong_goi_openai_lan_nua(redis, wired):
    published, fake = wired
    message = question()
    await consumer._process_record(record(message))
    await consumer._process_record(record(message))

    assert len(fake.calls) == 1
    # Phát lại lần 2: có thể lần 1 chết giữa save_response và publish
    assert [r.request_id for r in published] == [message.request_id] * 2


async def test_instance_chet_giua_chung_thi_lan_giao_lai_van_duoc_xu_ly(redis, wired):
    """Chưa có câu trả lời cache = lần trước chưa xong -> phải xử lý lại, không
    được bỏ qua (bỏ qua là user mất lượt và SSE chờ mãi)."""
    published, fake = wired
    message = question()
    # Mô phỏng: lần trước đã gọi OpenAI nhưng bị kill trước khi cache/publish
    await consumer._process_record(record(message))
    await redis.delete(f"resp:{message.request_id}")

    await consumer._process_record(record(message))
    assert len(fake.calls) == 2
    assert published[-1].status == "ok"


async def test_bo_dem_replay_chi_ghi_mot_lan_moi_cau_tra_loi(redis, wired):
    """Ghi ở response_consumer (chạy trên MỌI instance) là mỗi response bị ghi N
    lần với N instance."""
    message = question()
    await consumer._process_record(record(message))
    buffered = await replay.missed_since(redis, 1, uuid4())
    assert [r.request_id for r in buffered] == [message.request_id]


class FakeKafkaConsumer:
    """Thay AIOKafkaConsumer: phát lần lượt các record, ghi lại lần commit."""

    def __init__(self, records):
        self.records = list(records)
        self.commits = 0
        self.stopped = False

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True

    async def commit(self):
        self.commits += 1

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.records:
            return self.records.pop(0)
        await asyncio.Event().wait()  # topic yên: chờ mãi như Kafka thật


async def test_loi_ha_tang_khong_lam_chet_vong_lap_consumer(redis, wired, monkeypatch):
    """Trước đây chỉ bắt ValidationError: Redis rớt 1 giây là task consumer chết
    im lặng, API vẫn nhận câu hỏi và trừ quota nhưng không ai trả lời."""
    published, _ = wired
    first, second = question(), question()
    fake = FakeKafkaConsumer([record(first, 0), record(second, 1)])
    monkeypatch.setattr(consumer, "AIOKafkaConsumer", lambda *a, **k: fake)
    monkeypatch.setattr(consumer, "INFRA_RETRY_DELAY_SECONDS", 0)

    real_get_cached = idempotency.get_cached_response
    failures = {"left": 2}

    async def flaky(redis_client, request_id):
        if failures["left"]:
            failures["left"] -= 1
            raise ConnectionError("Redis rớt")
        return await real_get_cached(redis_client, request_id)

    monkeypatch.setattr(idempotency, "get_cached_response", flaky)

    consumer.start_consumer()
    try:
        for _ in range(200):
            if fake.commits == 2:
                break
            await asyncio.sleep(0.01)
        assert consumer.is_alive()
    finally:
        await consumer.stop_consumer()

    assert fake.commits == 2
    assert [r.request_id for r in published] == [first.request_id, second.request_id]


async def test_tat_consumer_khi_dang_ranh_khong_bi_treo(redis, wired, monkeypatch):
    """Trước đây vòng lặp chỉ xem cờ dừng khi có message MỚI: topic yên là
    shutdown treo tới khi bị SIGKILL."""
    fake = FakeKafkaConsumer([])
    monkeypatch.setattr(consumer, "AIOKafkaConsumer", lambda *a, **k: fake)
    consumer.start_consumer()
    await asyncio.sleep(0.01)
    await asyncio.wait_for(consumer.stop_consumer(), timeout=1)
    assert fake.stopped


async def test_loi_khong_tu_het_thi_chuyen_dlq_thay_vi_ket_ca_partition(
    redis, wired, monkeypatch
):
    published, _ = wired
    dead = []

    async def fake_dlq(message):
        dead.append(message)

    broken, after = question(), question()
    fake = FakeKafkaConsumer([record(broken, 0), record(after, 1)])
    monkeypatch.setattr(consumer, "AIOKafkaConsumer", lambda *a, **k: fake)
    monkeypatch.setattr(consumer, "INFRA_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(consumer, "publish_dead_letter", fake_dlq)

    real = consumer._handle

    async def handle(message):
        if message.request_id == broken.request_id:
            raise RuntimeError("lỗi code, lần nào cũng vậy")
        await real(message)

    monkeypatch.setattr(consumer, "_handle", handle)
    consumer.start_consumer()
    try:
        for _ in range(200):
            if fake.commits == 2:
                break
            await asyncio.sleep(0.01)
    finally:
        await consumer.stop_consumer()

    assert [d.reason for d in dead] == ["processing_failed"]
    assert dead[0].attempts == consumer.INFRA_MAX_ATTEMPTS
    # Message bị bỏ cuộc vẫn báo lỗi xuống SSE để user không chờ mãi
    assert [(r.request_id, r.status) for r in published] == [
        (broken.request_id, "error"),
        (after.request_id, "ok"),
    ]
