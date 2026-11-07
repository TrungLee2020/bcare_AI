import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import v1
from app.config import settings
from app.redis_client import set_redis
from app.services import disclaimer
from app.sse import hub
from tests.test_v1_api import ANSWER, JWT_SECRET, SUPABASE_URL, USER, FakePipeline, auth, chat_body, sse_events
from tests.test_v1_history import turn

JSON = {"Accept": "application/json"}
TEXT = "Thông tin mang tính tham khảo, không thay thế khám chữa bệnh."
WITH_DISCLAIMER = ANSWER + " Đây là thông tin tham khảo, không thay thế khám chữa bệnh trực tiếp."


@pytest.fixture(autouse=True)
def v1_settings(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", SUPABASE_URL)
    monkeypatch.setattr(settings, "supabase_jwt_secret", JWT_SECRET)
    monkeypatch.setattr(settings, "consent_required", False)
    monkeypatch.setattr(settings, "v1_answer_timeout_seconds", 3.0)
    monkeypatch.setattr(v1, "DELTA_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(v1, "POLL_SECONDS", 0.05)
    hub.reset()
    yield
    hub.reset()


@pytest.fixture
def pipeline(monkeypatch, redis):
    fake = FakePipeline(redis)
    monkeypatch.setattr("app.api.v1.publish_chat_request", fake)
    return fake


@pytest_asyncio.fixture
async def client(redis, db_maker):
    set_redis(redis)
    app = FastAPI()
    v1.install(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    set_redis(None)


async def test_json_answer_has_disclaimer(client, pipeline):
    body = (await client.post("/v1/chat", json=chat_body(), headers={**auth(), **JSON})).json()
    assert body["disclaimer"] == TEXT


async def test_sse_done_has_disclaimer(client, pipeline):
    resp = await client.post("/v1/chat", json=chat_body(), headers=auth())
    assert sse_events(resp.text)[-1]["disclaimer"] == TEXT


async def test_greeting_and_crisis_have_disclaimer(client, pipeline):
    greeting = (await client.post("/v1/chat", json=chat_body("chào bạn"), headers={**auth(), **JSON})).json()
    crisis = (await client.post("/v1/chat", json=chat_body(client_safety="crisis"), headers={**auth(), **JSON})).json()
    assert greeting["disclaimer"] == TEXT and crisis["disclaimer"] == TEXT


async def test_answer_disclaimer_sentence_removed_only_when_app_shows_it(client, pipeline, monkeypatch):
    monkeypatch.setattr("tests.test_v1_api.ANSWER", WITH_DISCLAIMER)
    monkeypatch.setattr(settings, "chat_disclaimer_in_app", True)
    body = (await client.post("/v1/chat", json=chat_body(), headers={**auth(), **JSON})).json()
    assert body["reply"] == ANSWER


async def test_answer_keeps_disclaimer_sentence_when_flag_off(client, pipeline, monkeypatch):
    monkeypatch.setattr("tests.test_v1_api.ANSWER", WITH_DISCLAIMER)
    monkeypatch.setattr(settings, "chat_disclaimer_in_app", False)
    body = (await client.post("/v1/chat", json=chat_body(), headers={**auth(), **JSON})).json()
    assert body["reply"] == WITH_DISCLAIMER


async def test_sse_deltas_strip_disclaimer_when_flag_on(client, pipeline, monkeypatch):
    monkeypatch.setattr("tests.test_v1_api.ANSWER", WITH_DISCLAIMER)
    monkeypatch.setattr(settings, "chat_disclaimer_in_app", True)
    resp = await client.post("/v1/chat", json=chat_body(), headers=auth())
    text = "".join(e["text"] for e in sse_events(resp.text) if e["type"] == "delta")
    assert text == ANSWER


async def test_reply_never_empty_when_answer_is_only_disclaimer(client, pipeline, monkeypatch):
    only = "Đây là thông tin tham khảo, không thay thế khám chữa bệnh trực tiếp."
    monkeypatch.setattr("tests.test_v1_api.ANSWER", only)
    monkeypatch.setattr(settings, "chat_disclaimer_in_app", True)
    body = (await client.post("/v1/chat", json=chat_body(), headers={**auth(), **JSON})).json()
    assert body["reply"] == only


async def test_history_assistant_has_disclaimer_user_does_not(client):
    sid = __import__("uuid").uuid4()
    await turn(sid, "Tôi bị đau đầu 3 ngày")
    resp = await client.get(f"/v1/conversations/{sid}/messages", headers=auth())
    msgs = resp.json()["messages"]
    assistant = [m for m in msgs if m["role"] == "assistant"]
    users = [m for m in msgs if m["role"] == "user"]
    assert assistant and users
    assert all(m["disclaimer"] == TEXT for m in assistant)
    assert all("disclaimer" not in m for m in users)
