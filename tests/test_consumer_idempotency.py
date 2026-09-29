"""
Kafka giao lại message khi consumer chết trước lúc commit offset. Dấu "đã xử
lý" là câu trả lời đã cache — không phải một cờ "đang xử lý" đặt từ đầu, vì cờ
kiểu đó làm mất luôn message của instance bị kill giữa chừng.
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiokafka import TopicPartition

from app.kafka import consumer
from app.redis_client import set_redis
from app.schemas import ChatRequestMessage
from app.services import idempotency, openai_client
from app.sse import replay
from tests.fake_openai import FakeOpenAI


def record(message: ChatRequestMessage, offset: int = 0, partition: int = 0):
    return SimpleNamespace(
        value=message.model_dump_json().encode(),
        topic="chat_requests",
        partition=partition,
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
    """Thay AIOKafkaConsumer: phát các record theo partition qua getmany(),
    ghi lại offset đã commit của từng partition."""

    def __init__(self, records):
        self.pending = list(records)
        self.committed: dict[int, int] = {}
        self.commit_calls = 0
        self.stopped = False
        self.paused: set = set()

    @property
    def commits(self) -> int:
        return self.commit_calls

    def subscribe(self, topics, listener=None):
        self.listener = listener

    async def start(self):
        pass

    async def stop(self):
        self.stopped = True

    async def commit(self, offsets):
        self.commit_calls += 1
        for tp, offset in offsets.items():
            self.committed[tp.partition] = offset

    def pause(self, *tps):
        self.paused.update(tps)

    def resume(self, *tps):
        self.paused.difference_update(tps)

    async def getmany(self, timeout_ms=0):
        if not self.pending:
            await asyncio.sleep(min(timeout_ms, 20) / 1000)  # topic yên
            return {}
        batch: dict = {}
        for r in self.pending:
            batch.setdefault(TopicPartition(r.topic, r.partition), []).append(r)
        self.pending = []
        return batch


async def wait_until(condition, seconds: float = 3.0):
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Hết giờ chờ")


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
        await wait_until(lambda: fake.commits == 2)
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
    await asyncio.wait_for(consumer.stop_consumer(), timeout=2)
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
        await wait_until(lambda: fake.commits == 2)
    finally:
        await consumer.stop_consumer()

    assert [d.reason for d in dead] == ["processing_failed"]
    assert dead[0].attempts == consumer.INFRA_MAX_ATTEMPTS
    # Message bị bỏ cuộc vẫn báo lỗi xuống SSE để user không chờ mãi
    assert [(r.request_id, r.status) for r in published] == [
        (broken.request_id, "error"),
        (after.request_id, "ok"),
    ]


async def test_partition_cham_khong_chan_partition_khac(redis, wired, monkeypatch):
    """Mục tiêu của worker theo partition: trước đây cả instance xử lý tuần tự,
    một câu chậm (retry OpenAI) làm mọi user trên instance cùng chờ."""
    published, _ = wired
    slow, fast = question(), question()
    release = asyncio.Event()
    real = consumer._handle

    async def handle(message):
        if message.request_id == slow.request_id:
            await release.wait()
        await real(message)

    monkeypatch.setattr(consumer, "_handle", handle)
    fake = FakeKafkaConsumer([record(slow, 0, partition=0), record(fast, 0, partition=1)])
    monkeypatch.setattr(consumer, "AIOKafkaConsumer", lambda *a, **k: fake)
    consumer.start_consumer()
    try:
        await wait_until(lambda: fake.committed.get(1) == 1)
        assert [r.request_id for r in published] == [fast.request_id]
        assert 0 not in fake.committed  # câu chậm chưa xong thì chưa commit
        release.set()
        await wait_until(lambda: fake.committed.get(0) == 1)
    finally:
        await consumer.stop_consumer()


async def test_trong_mot_partition_van_dung_thu_tu(redis, wired, monkeypatch):
    """Partition key = user_id: câu hỏi của cùng một user phải được trả lời
    đúng thứ tự đã gửi."""
    published, _ = wired
    questions = [question() for _ in range(5)]
    fake = FakeKafkaConsumer([record(q, i, partition=0) for i, q in enumerate(questions)])
    monkeypatch.setattr(consumer, "AIOKafkaConsumer", lambda *a, **k: fake)
    consumer.start_consumer()
    try:
        await wait_until(lambda: fake.committed.get(0) == 5)
    finally:
        await consumer.stop_consumer()
    assert [r.request_id for r in published] == [q.request_id for q in questions]


async def test_gioi_han_so_cau_xu_ly_cung_luc(redis, wired, monkeypatch):
    """Trần theo rate limit OpenAI: 4 partition nhưng chỉ cho 2 câu chạy cùng lúc."""
    monkeypatch.setattr(consumer.settings, "max_concurrent_answers", 2)
    running = {"now": 0, "max": 0}
    real = consumer._handle

    async def handle(message):
        running["now"] += 1
        running["max"] = max(running["max"], running["now"])
        await asyncio.sleep(0.05)
        running["now"] -= 1
        await real(message)

    monkeypatch.setattr(consumer, "_handle", handle)
    fake = FakeKafkaConsumer([record(question(), 0, partition=p) for p in range(4)])
    monkeypatch.setattr(consumer, "AIOKafkaConsumer", lambda *a, **k: fake)
    consumer.start_consumer()
    try:
        await wait_until(lambda: len(fake.committed) == 4)
    finally:
        await consumer.stop_consumer()
    assert running["max"] == 2


async def test_partition_bi_thu_hoi_thi_dung_worker_cua_no(redis, wired, monkeypatch):
    """Rebalance: partition chuyển sang instance khác thì worker ở đây phải
    dừng, nếu không 2 instance cùng xử lý 1 partition."""
    fake = FakeKafkaConsumer([])
    monkeypatch.setattr(consumer, "AIOKafkaConsumer", lambda *a, **k: fake)
    consumer.start_consumer()
    try:
        await wait_until(lambda: hasattr(fake, "listener"))
        tp = TopicPartition("chat_requests", 3)
        worker = fake.listener.get(tp)
        await fake.listener.on_partitions_revoked([tp])
        assert worker.task.done()
        assert tp not in fake.listener.by_tp
    finally:
        await consumer.stop_consumer()


async def test_partition_don_viec_thi_tam_ngung_doc(monkeypatch):
    monkeypatch.setattr(consumer.settings, "kafka_max_buffered_per_partition", 4)
    fake = FakeKafkaConsumer([])
    workers = consumer._Workers(fake, asyncio.Semaphore(1))
    tp = TopicPartition("chat_requests", 0)
    worker = workers.get(tp)
    worker.task.cancel()  # không cho worker rút bớt hàng đợi trong test này
    for i in range(4):
        worker.queue.put_nowait(i)

    workers.apply_backpressure()
    assert tp in fake.paused
    for _ in range(3):
        worker.queue.get_nowait()
    workers.apply_backpressure()
    assert tp not in fake.paused


async def test_vong_doc_chet_thi_tu_khoi_dong_lai(redis, wired, monkeypatch):
    """Kafka chưa sẵn sàng lúc boot: trước đây task chết hẳn, API vẫn nhận câu
    hỏi và trừ quota nhưng không ai trả lời."""
    published, _ = wired
    message = question()
    healthy = FakeKafkaConsumer([record(message, 0)])
    attempts = {"n": 0}

    class BrokenOnce(FakeKafkaConsumer):
        async def start(self):
            raise ConnectionError("Kafka chưa sẵn sàng")

    def factory(*a, **k):
        attempts["n"] += 1
        return BrokenOnce([]) if attempts["n"] == 1 else healthy

    monkeypatch.setattr(consumer, "AIOKafkaConsumer", factory)
    monkeypatch.setattr(consumer, "RESTART_DELAY_SECONDS", 0.01)

    consumer.start_consumer()
    try:
        await wait_until(lambda: healthy.commits == 1)
        assert consumer.is_alive()
    finally:
        await consumer.stop_consumer()

    assert attempts["n"] == 2
    assert [r.request_id for r in published] == [message.request_id]


def test_consumer_dat_rebalance_timeout_lon_hon_thoi_gian_cho_message_do():
    assert consumer.REBALANCE_TIMEOUT_MS > consumer.SHUTDOWN_GRACE_SECONDS * 1000


def test_openai_client_khong_tu_retry_chong_len_call_with_backoff():
    from app.services import openai_client

    openai_client.start_openai()
    try:
        assert openai_client.get_openai().max_retries == 0
    finally:
        openai_client._client = None
