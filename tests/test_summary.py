from uuid import uuid4

import pytest

from app.config import settings
from app.db import repository
from app.db.session import db_session
from app.prompts import load_system_prompt
from app.services import history, openai_client, summarizer
from tests.fake_openai import FakeOpenAI, make_summary
from tests.test_history import request, response


@pytest.fixture
def fake_openai():
    client = FakeOpenAI()
    openai_client.set_openai(client)
    yield client
    openai_client.set_openai(None)


async def fill(session_id, turns: int):
    for i in range(turns):
        msg = request(session_id, f"câu hỏi {i}")
        await history.record_turn(msg, response(msg, f"trả lời {i}"))


async def test_chua_du_nguong_thi_khong_tom_tat(db_maker, fake_openai, monkeypatch):
    monkeypatch.setattr(settings, "summary_trigger_messages", 20)
    sid = uuid4()
    await fill(sid, 3)  # 6 message < 20

    assert fake_openai.calls == []
    ctx = await history.load_context(request(sid))
    assert ctx.summary is None


async def test_du_nguong_thi_tom_tat_va_luu_moc(db_maker, fake_openai, monkeypatch):
    monkeypatch.setattr(settings, "summary_trigger_messages", 6)
    monkeypatch.setattr(settings, "history_window_messages", 2)
    sid = uuid4()
    await fill(sid, 4)  # 8 message >= 6

    async with db_session() as db:
        session = await db.get(repository.ChatSession, sid)
        assert session.summary == make_summary().summary
        assert session.summarized_through_id is not None


async def test_sau_khi_tom_tat_prompt_khong_cho_lai_phan_da_nen(
    db_maker, fake_openai, monkeypatch
):
    """Đây là toàn bộ mục đích của summary: lịch sử dài ra nhưng phần chở
    nguyên văn trong prompt thì không."""
    monkeypatch.setattr(settings, "summary_trigger_messages", 6)
    monkeypatch.setattr(settings, "history_window_messages", 2)
    sid = uuid4()
    await fill(sid, 4)

    ctx = await history.load_context(request(sid))
    assert ctx.summary
    assert len(ctx.recent) <= 2
    assert "câu hỏi 0" not in [m.content for m in ctx.recent]


async def test_summary_lo_system_prompt_thi_khong_duoc_ghi(db_maker, monkeypatch):
    """Summary được nạp lại vào prompt của MỌI câu hỏi sau trong phiên — một
    summary nhiễm sẽ đầu độc cả phần còn lại của hội thoại."""
    monkeypatch.setattr(settings, "summary_trigger_messages", 6)
    monkeypatch.setattr(settings, "history_window_messages", 2)
    leaked = make_summary(summary=load_system_prompt(settings.prompt_version)[:400])
    openai_client.set_openai(FakeOpenAI(summary=leaked))
    sid = uuid4()
    try:
        await fill(sid, 4)
    finally:
        openai_client.set_openai(None)

    ctx = await history.load_context(request(sid))
    assert ctx.summary is None


async def test_summary_qua_dai_thi_khong_duoc_ghi(db_maker, monkeypatch):
    monkeypatch.setattr(settings, "summary_trigger_messages", 6)
    monkeypatch.setattr(settings, "history_window_messages", 2)
    monkeypatch.setattr(settings, "summary_max_chars", 100)
    openai_client.set_openai(FakeOpenAI(summary=make_summary(summary="x" * 500)))
    sid = uuid4()
    try:
        await fill(sid, 4)
    finally:
        openai_client.set_openai(None)

    assert (await history.load_context(request(sid))).summary is None


async def test_tom_tat_loi_thi_khong_lam_hong_luot_tra_loi(db_maker, monkeypatch):
    monkeypatch.setattr(settings, "summary_trigger_messages", 6)
    monkeypatch.setattr(settings, "history_window_messages", 2)
    sid = uuid4()
    await fill(sid, 3)

    openai_client.set_openai(FakeOpenAI(error=RuntimeError("API down")))
    try:
        msg = request(sid, "câu hỏi cuối")
        await history.record_turn(msg, response(msg))  # không được raise
    finally:
        openai_client.set_openai(None)

    ctx = await history.load_context(request(sid))
    assert ctx.summary is None
    assert any(m.content == "câu hỏi cuối" for m in ctx.recent)
