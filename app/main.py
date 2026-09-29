import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response, status

from app import metrics
from app.api.chat import router as chat_router
from app.sse.stream import router as sse_router
from app.config import settings
from app.db.session import start_db, stop_db
from app.kafka import consumer, response_consumer
from app.kafka.topics import verify_production_topics
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


# HMAC-SHA256 cần secret ít nhất 32 byte ngẫu nhiên; secret ngắn đoán được.
MIN_SECRET_LENGTH = 32


def production_config_problems() -> list[str]:
    """Những thứ không được phép ở production. Rỗng = đạt."""
    problems = []
    if not settings.auth_required:
        problems.append("AUTH_REQUIRED phải là true")
    if len(settings.auth_secret) < MIN_SECRET_LENGTH:
        problems.append(f"AUTH_SECRET phải dài ít nhất {MIN_SECRET_LENGTH} ký tự")
    if settings.enable_test_endpoints:
        problems.append("ENABLE_TEST_ENDPOINTS phải là false (endpoint test bỏ qua quota)")
    if not settings.metrics_token:
        problems.append("METRICS_TOKEN phải được đặt (/metrics lộ chi phí và lưu lượng)")
    if not settings.openai_api_key:
        problems.append("OPENAI_API_KEY trống")
    return problems


def _check_production_config() -> None:
    problems = production_config_problems()
    if problems:
        raise RuntimeError(
            "APP_ENV=production nhưng cấu hình chưa đạt: " + "; ".join(problems)
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
    if settings.app_env == "production":
        _check_production_config()
        await verify_production_topics()
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


# Production không public /docs, /redoc, /openapi.json: bản đồ API đầy đủ chỉ
# giúp người dò tìm, còn hệ thống tích hợp đã có tài liệu riêng.
_public_docs = settings.app_env != "production"
app = FastAPI(
    title="bcare_AI - AI answering service",
    lifespan=lifespan,
    docs_url="/docs" if _public_docs else None,
    redoc_url="/redoc" if _public_docs else None,
    openapi_url="/openapi.json" if _public_docs else None,
)
app.include_router(chat_router)
app.include_router(sse_router)


@app.get("/health")
async def health(response: Response) -> dict:
    """
    Dùng làm liveness probe. Trả 503 khi một trong hai vòng consumer đã chết:
    process vẫn nhận HTTP bình thường nhưng không còn trả lời câu hỏi (hoặc
    không còn đẩy được xuống SSE) — để k8s khởi động lại thay vì đứng im.
    """
    checks = {
        "request_consumer": consumer.is_alive(),
        "response_consumer": response_consumer.is_alive(),
    }
    ok = all(checks.values())
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if ok else "degraded", **checks}


@app.get("/metrics")
async def read_metrics(request: Request) -> dict:
    """Số liệu của RIÊNG instance này, reset khi restart. Đủ để theo dõi giai
    đoạn rollout; mở rộng thì thay bằng Prometheus exporter.

    Cần `Authorization: Bearer <METRICS_TOKEN>` khi METRICS_TOKEN được đặt
    (production bắt buộc đặt): số liệu này lộ chi phí và lưu lượng thật.
    """
    if settings.metrics_token:
        header = request.headers.get("authorization", "")
        given = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(given.encode(), settings.metrics_token.encode()):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
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
