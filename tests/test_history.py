from uuid import uuid4

import pytest

from app.config import settings
from app.db import repository
from app.db.session import db_session
from app.schemas import ChatRequestMessage, ChatResponseMessage
from app.services import history, openai_client
from tests.fake_openai import FakeOpenAI, make_answer


def request(session_id, content="Tôi bị đau đầu", user_id=1) -> ChatRequestMessage:
    return ChatRequestMessage(
        request_id=uuid4(), user_id=user_id, session_id=session_id, tier="free",
        content=content,
    )


def response(message, answer_text="Bạn nên nghỉ ngơi và theo dõi thêm.") -> ChatResponseMessage:
    return ChatResponseMessage(
        request_id=message.request_id, user_id=message.user_id,
        session_id=message.session_id, status="ok",
        answer=make_answer(answer=answer_text),
        prompt_version=settings.prompt_version, model="fake",
    )


async def test_ghi_va_nap_lai_duoc_mot_luot(db_maker):
    sid = uuid4()
    msg = request(sid, "Tôi bị đau đầu 3 ngày")
    await history.record_turn(msg, response(msg, "Bạn nên nghỉ ngơi nhé"))

    ctx = await history.load_context(request(sid))
    assert [m.role for m in ctx.recent] == ["user", "assistant"]
    assert ctx.recent[0].content == "Tôi bị đau đầu 3 ngày"
    assert ctx.recent[1].content == "Bạn nên nghỉ ngơi nhé"


async def test_lich_su_giu_dung_thu_tu_cu_den_moi(db_maker):
    sid = uuid4()
    for i in range(3):
        msg = request(sid, f"câu hỏi {i}")
        await history.record_turn(msg, response(msg, f"trả lời {i}"))

    ctx = await history.load_context(request(sid))
    assert [m.content for m in ctx.recent if m.role == "user"] == [
        "câu hỏi 0", "câu hỏi 1", "câu hỏi 2",
    ]


async def test_chi_nap_n_message_gan_nhat(db_maker, monkeypatch):
    monkeypatch.setattr(settings, "history_window_messages", 4)
    sid = uuid4()
    for i in range(5):
        msg = request(sid, f"câu hỏi {i}")
        await history.record_turn(msg, response(msg, f"trả lời {i}"))

    ctx = await history.load_context(request(sid))
    assert len(ctx.recent) == 4
    # 4 cái MỚI NHẤT, không phải 4 cái cũ nhất
    assert ctx.recent[-1].content == "trả lời 4"


async def test_khong_co_session_id_thi_khong_co_ngu_canh(db_maker):
    msg = ChatRequestMessage(user_id=1, tier="free", content="xin chào")
    await history.record_turn(msg, response(msg))
    assert (await history.load_context(msg)).is_empty


async def test_xu_ly_lai_cung_request_id_khong_nhan_doi_lich_su(db_maker):
    """Nếu Redis mất dữ liệu và Kafka giao lại message, unique index
    (request_id, role) phải chặn việc ghi trùng vào lịch sử."""
    sid = uuid4()
    msg = request(sid)
    await history.record_turn(msg, response(msg))
    await history.record_turn(msg, response(msg))

    ctx = await history.load_context(request(sid))
    assert len(ctx.recent) == 2


async def test_hai_phien_khong_lan_ngu_canh_vao_nhau(db_maker):
    a, b = uuid4(), uuid4()
    msg_a = request(a, "chuyện phiên A")
    await history.record_turn(msg_a, response(msg_a))

    assert (await history.load_context(request(b))).is_empty
