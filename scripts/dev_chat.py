"""
Server test chat nhanh ở local — KHÔNG dùng cho production.

Mô hình: mỗi user_id được hỏi `QUOTA_FREE_PER_DAY` câu/ngày (mặc định 2).
Câu thứ 1 trả lời bằng DEV_MODEL_FIRST, các câu sau bằng DEV_MODEL_NEXT. Hết
lượt thì đổi user_id khác để hỏi tiếp.

Gọi thẳng đúng logic của hệ thống thật: quota (app/services/quota.py, chạy trên
fakeredis) -> lọc injection -> ngữ cảnh -> OpenAI -> validate output -> lưu
lịch sử + tóm tắt. Bỏ Kafka, SSE, auth. Lịch sử lưu ở SQLite `dev_chat.db`;
quota nằm trong RAM nên restart server là reset lượt hỏi.

    pip install -r requirements-dev.txt
    # đặt OPENAI_API_KEY trong .env
    python -m scripts.dev_chat          # mở http://localhost:8001

Tuỳ chọn trong .env.dev (hoặc biến môi trường):
    DEV_MODEL_FIRST=gpt-5.6-luna        DEV_PRICE_FIRST=in,out     (USD/1M token)
    DEV_MODEL_NEXT=gpt-5-nano           DEV_PRICE_NEXT=0.05,0.4
"""

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import fakeredis.aioredis
from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models import Base, ChatMessage
from app.db.session import db_session, set_sessionmaker
from app.schemas import ChatRequestMessage
from app.services import answering, history, quota
from app.services.openai_client import start_openai, stop_openai

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# Biến DEV_* để ở .env.dev cho gọn, tách khỏi cấu hình thật của app ở .env
_env = {**dotenv_values(".env.dev"), **os.environ}
DB_URL = _env.get("DEV_CHAT_DB_URL", "sqlite+aiosqlite:///dev_chat.db")
HTML = Path(__file__).with_name("dev_chat.html")
TIER = "free"


def _price(raw: str | None) -> tuple[float, float] | None:
    """"in,out" USD / 1M token. Không khai báo thì không tính chi phí — thà
    không hiện còn hơn hiện số sai."""
    if not raw:
        return None
    inp, out = raw.split(",")
    return float(inp), float(out)


MODELS = {
    "first": {
        "model": _env.get("DEV_MODEL_FIRST", "gpt-5.6-luna"),
        "price": _price(_env.get("DEV_PRICE_FIRST")),
    },
    "next": {
        "model": _env.get("DEV_MODEL_NEXT", "gpt-5-nano"),
        "price": _price(_env.get("DEV_PRICE_NEXT")),
    },
}

_redis = None


def _model_for(question_no: int) -> dict:
    return MODELS["first"] if question_no == 1 else MODELS["next"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY trống — đặt trong .env trước khi chạy")
    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    set_sessionmaker(async_sessionmaker(engine, expire_on_commit=False))
    _redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    start_openai()
    logging.info("Dev chat: câu 1=%s, câu sau=%s, %d câu/user, db=%s",
                 MODELS["first"]["model"], MODELS["next"]["model"],
                 settings.quota_limit_for_tier(TIER), DB_URL)
    yield
    await stop_openai()
    await _redis.aclose()
    set_sessionmaker(None)
    await engine.dispose()


app = FastAPI(title="bcare_AI dev chat", lifespan=lifespan)
# Cho phép mở dev_chat.html trực tiếp (Live Server / file://) mà vẫn gọi được API.
# Chỉ lắng nghe 127.0.0.1 nên mở CORS ở đây không lộ ra ngoài.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class DevAsk(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    session_id: UUID
    user_id: int


async def _quota_info(user_id: int) -> dict:
    limit = settings.quota_limit_for_tier(TIER)
    remaining = await quota.remaining(_redis, user_id, TIER)
    return {"limit": limit, "remaining": remaining, "used": limit - remaining}


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(HTML)


@app.get("/config")
async def config() -> dict:
    return {
        "prompt_version": settings.prompt_version,
        "models": {k: v["model"] for k, v in MODELS.items()},
        "limit": settings.quota_limit_for_tier(TIER),
    }


@app.get("/quota/{user_id}")
async def get_quota(user_id: int) -> dict:
    return await _quota_info(user_id)


@app.post("/ask")
async def ask(body: DevAsk):
    try:
        remaining = await quota.consume(_redis, body.user_id, TIER)
    except quota.QuotaExceeded as exc:
        return JSONResponse(status_code=429, content={"detail": str(exc), **await _quota_info(body.user_id)})

    limit = settings.quota_limit_for_tier(TIER)
    question_no = limit - remaining
    cfg = _model_for(question_no)
    message = ChatRequestMessage(
        request_id=uuid4(),
        user_id=body.user_id,
        session_id=body.session_id,
        tier=TIER,
        content=body.content,
    )
    started = time.perf_counter()
    try:
        context = await history.load_context(message)
        response = await answering.answer_question(message, context, cfg["model"])
        await history.record_turn(message, response)
    except Exception as exc:
        # Lỗi hệ thống = user chưa được trả lời, hoàn lượt như luồng thật
        await quota.refund(_redis, body.user_id)
        logging.exception("Lỗi khi trả lời (%s)", cfg["model"])
        return JSONResponse(
            status_code=502,
            content={"detail": f"{type(exc).__name__}: {exc}", **await _quota_info(body.user_id)},
        )

    data = response.model_dump(mode="json")
    usage = data.get("usage") or {}
    if usage:
        # cost.py chỉ có 1 bảng giá (của OPENAI_MODEL); tính lại theo giá model này
        if cfg["price"] is None:
            usage["cost_usd"] = None
        else:
            pin, pout = cfg["price"]
            usage["cost_usd"] = round(
                (usage["prompt_tokens"] * pin + usage["completion_tokens"] * pout) / 1_000_000, 6
            )
    data["ms"] = round((time.perf_counter() - started) * 1000)
    data["question_no"] = question_no
    data["quota"] = {"limit": limit, "remaining": remaining, "used": question_no}
    return data


@app.get("/history/{session_id}")
async def get_history(session_id: UUID) -> list[dict]:
    """Nạp lại hội thoại cũ của 1 phiên."""
    async with db_session() as db:
        rows = await db.scalars(
            select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.id)
        )
        return [{"role": r.role, "content": r.content, "meta": r.meta} for r in rows]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(_env.get("DEV_CHAT_PORT", "8001")))
