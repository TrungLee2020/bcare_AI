import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.kafka.consumer import start_consumer, stop_consumer
from app.kafka.producer import publish_chat_request, start_producer, stop_producer
from app.schemas import ChatRequestMessage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await start_producer()
    start_consumer()
    yield
    await stop_consumer()
    await stop_producer()


app = FastAPI(title="bcare_AI - AI answering service", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/test/enqueue", status_code=202)
async def test_enqueue(message: ChatRequestMessage) -> dict:
    """
    Endpoint tạm thời chỉ để verify pipeline Phase 1 (thứ tự message + partition
    theo user_id). KHÔNG phải endpoint chính thức cho FE — endpoint thật (có
    check quota, idempotency) sẽ được thêm ở Phase 2.
    """
    await publish_chat_request(message)
    return {"request_id": str(message.request_id), "status": "enqueued"}
