import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.chat import router as chat_router
from app.config import settings
from app.kafka.consumer import start_consumer, stop_consumer
from app.kafka.producer import publish_chat_request, start_producer, stop_producer
from app.redis_client import start_redis, stop_redis
from app.schemas import ChatRequestMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_redis()
    await start_producer()
    start_consumer()
    yield
    await stop_consumer()
    await stop_producer()
    await stop_redis()


app = FastAPI(title="bcare_AI - AI answering service", lifespan=lifespan)
app.include_router(chat_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


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
