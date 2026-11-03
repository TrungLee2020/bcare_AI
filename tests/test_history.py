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


async def test_khong_doc_duoc_phien_cua_user_khac(db_maker):
    """session_id do client gửi lên: không lọc theo chủ phiên thì ai có
    session_id của người khác cũng đọc được lịch sử sức khoẻ của họ."""
    sid = uuid4()
    owner = request(sid, "tôi bị trầm cảm", user_id=1)
    await history.record_turn(owner, response(owner))

    assert (await history.load_context(request(sid, user_id=2))).is_empty
    assert not (await history.load_context(request(sid, user_id=1))).is_empty


async def test_khong_ghi_duoc_vao_phien_cua_user_khac(db_maker):
    """Ghi được là chèn được nội dung (kể cả câu injection) vào ngữ cảnh của
    người khác."""
    sid = uuid4()
    owner = request(sid, "câu của chủ phiên", user_id=1)
    await history.record_turn(owner, response(owner))
    intruder = request(sid, "câu của người lạ", user_id=2)
    await history.record_turn(intruder, response(intruder))

    ctx = await history.load_context(request(sid, user_id=1))
    assert "câu của người lạ" not in [m.content for m in ctx.recent]


async def test_luu_should_see_doctor_vao_meta_de_do_ti_le_khuyen_di_kham(db_maker):
    import sqlalchemy

    from app.db.models import ChatMessage

    sid = uuid4()
    msg = request(sid)
    resp = response(msg)
    resp.answer = make_answer(answer="Bạn nên đi khám sớm.", should_see_doctor=True)
    await history.record_turn(msg, resp)

    async with db_maker() as db:
        meta = (await db.execute(
            sqlalchemy.select(ChatMessage.meta).where(ChatMessage.role == "assistant")
        )).scalar_one()
    assert meta["should_see_doctor"] is True


async def test_luot_moi_doc_duoc_ngay_ca_khi_dang_tom_tat(db_maker, monkeypatch):
    """Tóm tắt gọi OpenAI mất vài giây: lượt vừa ghi phải commit trước đó để
    app đọc lịch sử (/v1/conversations) thấy luôn."""
    from app.services import summarizer

    seen = []

    async def slow_summarize(db, session_id):
        async with db_maker() as other:
            seen.extend(await repository.list_messages(other, session_id, msg.user_id, 10))
        raise RuntimeError("tóm tắt hỏng")

    monkeypatch.setattr(summarizer, "maybe_summarize", slow_summarize)
    sid = uuid4()
    msg = request(sid, user_id="u1")
    with pytest.raises(RuntimeError):
        await history.record_turn(msg, response(msg))

    assert [m.role for m in seen] == ["user", "assistant"]
    ctx = await history.load_context(request(sid, user_id="u1"))
    assert len(ctx.recent) == 2


async def soft_delete(db_maker, sid, user_id):
    async with db_maker() as db:
        assert await repository.soft_delete_conversation(db, sid, user_id)
        await db.commit()


async def test_hoi_thoai_da_xoa_khong_con_la_ngu_canh(db_maker):
    sid = uuid4()
    msg = request(sid, "Tôi bị đau đầu", user_id="u1")
    await history.record_turn(msg, response(msg))
    await soft_delete(db_maker, sid, "u1")

    ctx = await history.load_context(request(sid, user_id="u1"))
    assert ctx.is_empty


async def test_khong_ghi_luot_moi_vao_hoi_thoai_da_xoa(db_maker):
    """User xoá trong lúc câu trả lời đang sinh: lượt đó không được ghi lại."""
    import sqlalchemy

    from app.db.models import ChatMessage

    sid = uuid4()
    first = request(sid, user_id="u1")
    await history.record_turn(first, response(first))
    await soft_delete(db_maker, sid, "u1")
    late = request(sid, "câu đến sau khi xoá", user_id="u1")
    await history.record_turn(late, response(late))

    async with db_maker() as db:
        contents = (await db.scalars(sqlalchemy.select(ChatMessage.content))).all()
    assert "câu đến sau khi xoá" not in contents


async def test_xoa_han_sau_thoi_han_kem_message_va_summary(db_maker):
    from datetime import datetime, timedelta, timezone

    import sqlalchemy

    from app.db.models import ChatMessage, ChatSession

    gone, kept, recent = uuid4(), uuid4(), uuid4()
    for sid in (gone, kept, recent):
        msg = request(sid, user_id="u1")
        await history.record_turn(msg, response(msg))
    await soft_delete(db_maker, recent, "u1")
    async with db_maker() as db:
        # Xoá từ 15 ngày trước: quá hạn 14 ngày
        old = datetime.now(timezone.utc) - timedelta(days=settings.history_purge_after_days + 1)
        await db.execute(sqlalchemy.update(ChatSession).where(ChatSession.id == gone).values(deleted_at=old))
        await db.commit()

    assert await history.purge_deleted() == 1

    async with db_maker() as db:
        sessions = set((await db.scalars(sqlalchemy.select(ChatSession.id))).all())
        message_sessions = set((await db.scalars(sqlalchemy.select(ChatMessage.session_id))).all())
    # Đã xoá nhưng chưa tới hạn thì vẫn còn (ẩn); chưa xoá thì nguyên vẹn
    assert sessions == {kept, recent}
    assert message_sessions == {kept, recent}
