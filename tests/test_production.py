"""
Các chốt chặn cho lúc chuyển từ môi trường test sang production với dữ liệu thật.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.kafka import consumer
from app.kafka.topics import layout_problems, retention_ms, topic_specs
from app.redis_client import set_redis
from app.schemas import ChatRequestMessage
from app.services import idempotency, openai_client, quota
from tests.fake_openai import FakeOpenAI
from tests.test_consumer_idempotency import record

# --- câu hỏi quá cũ trong topic ---------------------------------------------


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


def old_question(age: timedelta) -> ChatRequestMessage:
    return ChatRequestMessage(
        user_id=5, tier="free", content="Tôi bị đau đầu",
        created_at=datetime.now(timezone.utc) - age,
    )


async def test_cau_hoi_qua_cu_khong_goi_openai(redis, wired):
    """Consumer group mới đọc từ đầu topic (auto_offset_reset=earliest): dữ
    liệu test còn sót không được trả lời lại bằng tiền OpenAI thật."""
    published, fake = wired
    message = old_question(timedelta(seconds=settings.request_max_age_seconds + 60))
    await consumer._process_record(record(message))

    assert fake.calls == []
    assert [(r.status, r.detail) for r in published] == [("error", "expired")]
    # Có cache -> Kafka giao lại cũng không xử lý lại, API trả "done"
    assert await idempotency.get_cached_response(redis, message.request_id)


async def test_cau_hoi_con_moi_van_duoc_tra_loi(redis, wired):
    published, fake = wired
    await consumer._process_record(record(old_question(timedelta(seconds=5))))
    assert len(fake.calls) == 1 and published[-1].status == "ok"


async def test_cau_hoi_qua_cu_trong_ngay_thi_hoan_quota(redis, wired):
    await quota.consume(redis, 5, "free")
    message = old_question(timedelta(seconds=settings.request_max_age_seconds + 1))
    await consumer._process_record(record(message))
    assert await quota.remaining(redis, 5, "free") == settings.quota_free_per_day


async def test_khong_hoan_quota_cua_ngay_hom_truoc_vao_hom_nay(redis):
    """Counter của hôm qua đã hết hạn: trừ vào hôm nay là tặng thêm một lượt."""
    await quota.consume(redis, 6, "free")
    await quota.refund(redis, 6, charged_at=datetime.now(timezone.utc) - timedelta(days=2))
    assert await quota.remaining(redis, 6, "free") == settings.quota_free_per_day - 1


# --- topic Kafka --------------------------------------------------------------


def described(replicas: int, missing: str | None = None) -> list[dict]:
    return [
        {
            "error_code": 0,
            "topic": name,
            "partitions": [{"partition": i, "replicas": list(range(replicas))} for i in range(3)],
        }
        for name in retention_ms()
        if name != missing
    ]


def test_topic_du_ban_sao_thi_dat():
    assert layout_problems(described(3), min_replication=3) == []


def test_replication_factor_1_bi_chan_o_production():
    """RF=1: 1 broker chết là mất câu hỏi, và acks=all không cứu được."""
    problems = layout_problems(described(1), min_replication=3)
    assert len(problems) == 3 and "replication factor 1" in problems[0]


def test_thieu_topic_bi_chan():
    problems = layout_problems(
        described(3, missing=settings.kafka_topic_dead_letter), min_replication=3
    )
    assert problems == [f"topic {settings.kafka_topic_dead_letter} chưa tồn tại"]


def test_topic_tao_ra_co_retention_va_min_insync():
    specs = {t.name: t for t in topic_specs()}
    requests = specs[settings.kafka_topic_chat_requests]
    assert requests.topic_configs["retention.ms"] == str(24 * 3600 * 1000)
    assert "min.insync.replicas" in requests.topic_configs


# --- cấu hình khởi động -----------------------------------------------------------


@pytest.fixture
def production_settings(monkeypatch):
    values = dict(
        app_env="production", auth_required=True, auth_secret="x" * 32,
        enable_test_endpoints=False, metrics_token="m" * 32, openai_api_key="sk-test",
    )
    for key, value in values.items():
        monkeypatch.setattr(settings, key, value)


def test_cau_hinh_production_day_du_thi_dat(production_settings):
    assert main.production_config_problems() == []


@pytest.mark.parametrize(
    "key, value, needle",
    [
        ("auth_required", False, "AUTH_REQUIRED"),
        ("auth_secret", "ngan", "AUTH_SECRET"),
        ("enable_test_endpoints", True, "ENABLE_TEST_ENDPOINTS"),
        ("metrics_token", "", "METRICS_TOKEN"),
        ("openai_api_key", "", "OPENAI_API_KEY"),
    ],
)
def test_cau_hinh_cua_moi_truong_test_bi_chan_o_production(
    production_settings, monkeypatch, key, value, needle
):
    monkeypatch.setattr(settings, key, value)
    problems = main.production_config_problems()
    assert any(needle in p for p in problems)
    with pytest.raises(RuntimeError, match=needle):
        main._check_production_config()


# --- /metrics -----------------------------------------------------------------


def metrics_client() -> TestClient:
    # Gọi thẳng route, không chạy lifespan (không cần Kafka/Redis)
    return TestClient(main.app)


def test_metrics_can_token_khi_da_dat(monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "bi-mat")
    client = metrics_client()
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer sai"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer bi-mat"}).status_code == 200


def test_metrics_khong_can_token_o_dev(monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "")
    assert metrics_client().get("/metrics").status_code == 200


# --- dọn dữ liệu test -----------------------------------------------------------


async def test_script_don_du_lieu_tu_choi_chay_o_production(monkeypatch):
    from scripts import reset_test_data

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr("sys.argv", ["reset_test_data", "--yes"])
    with pytest.raises(SystemExit, match="production"):
        await reset_test_data.main()


async def test_script_chi_xoa_key_cua_app_trong_redis(redis, monkeypatch):
    """Redis có thể dùng chung với dịch vụ khác: không được FLUSHDB."""
    from scripts import reset_test_data

    await redis.set("quota:1:2026-09-28", 2)
    await redis.set("resp:abc", "{}")
    await redis.set("dich-vu-khac:session", "giu lai")
    monkeypatch.setattr(reset_test_data.Redis, "from_url", lambda *a, **k: redis)
    monkeypatch.setattr(redis, "aclose", _noop)

    await reset_test_data.reset_redis()
    assert await redis.keys("*") == ["dich-vu-khac:session"]


async def _noop():
    pass


def test_bien_la_trong_env_khong_lam_app_chet(tmp_path, monkeypatch):
    """`.env` dùng chung với docker-compose (POSTGRES_PASSWORD, APP_PORT...):
    cấm biến lạ là app, pytest và script chạy ngoài Docker đều không lên."""
    from app.config import Settings

    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=secret\nAPP_PORT=8000\nPROMPT_VERSION=v4\n")
    monkeypatch.chdir(tmp_path)
    assert Settings().prompt_version == "v4"
