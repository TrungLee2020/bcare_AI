import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import metrics
from app.api.chat import router as chat_router
from app.sse.stream import router as sse_router
from app.config import settings
from app.db.session import start_db, stop_db
from app.kafka.consumer import start_consumer, stop_consumer
from app.kafka.response_consumer import start_response_consumer, stop_response_consumer
from app.kafka.producer import publish_chat_request, start_producer, stop_producer
from app.redis_client import start_redis, stop_redis
from app.services.openai_client import start_openai, stop_openai
from app.schemas import ChatRequestMessage

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _check_auth_config() -> None:
    """Fail-closed: bật auth mà quên đặt secret thì phải chết ngay lúc khởi
    động, chứ không phải chạy ngon lành rồi chấp nhận mọi token."""
    if settings.auth_required and not settings.auth_secret:
        raise RuntimeError(
            "AUTH_SECRET trống trong khi AUTH_REQUIRED=true. "
            "Đặt AUTH_SECRET, hoặc AUTH_REQUIRED=false nếu đang chạy local."
        )
    if not settings.auth_required:
        logger.warning(
            "AUTH_REQUIRED=false: user_id/tier lấy từ query param, KHÔNG xác "
            "thực. Chỉ được dùng ở local."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_auth_config()
    await start_redis()
    await start_db()
    start_openai()
    await start_producer()
    start_consumer()
    start_response_consumer()
    yield
    await stop_response_consumer()
    await stop_consumer()
    await stop_producer()
    await stop_openai()
    await stop_db()
    await stop_redis()


app = FastAPI(title="bcare_AI - AI answering service", lifespan=lifespan)
app.include_router(chat_router)
app.include_router(sse_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/metrics")
async def read_metrics() -> dict:
    """Số liệu của RIÊNG instance này, reset khi restart. Đủ để theo dõi giai
    đoạn rollout; mở rộng thì thay bằng Prometheus exporter."""
    return metrics.snapshot()


if settings.enable_test_endpoints:

    @app.post("/test/enqueue", status_code=202)
    async def test_enqueue(message: ChatRequestMessage) -> dict:
        """
        Chỉ để verify pipeline Phase 1 (thứ tự message + partition theo user_id).
        BYPASS quota và idempotency -> chỉ bật ở local qua ENABLE_TEST_ENDPOINTS=true.
        Endpoint thật cho FE là POST /chat/ask.
        """
        await publish_chat_request(message)
        return {"request_id": str(message.request_id), "status": "enqueued"}
