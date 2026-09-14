import asyncio
import json
from uuid import uuid4

import pytest

from app.config import settings
from app.redis_client import set_redis
from app.schemas import ChatResponseMessage
from app.sse import hub, replay
from app.sse.stream import event_stream, format_event, stream
from tests.fake_openai import make_answer


def response(user_id=1, request_id=None, status="ok", text="câu trả lời") -> ChatResponseMessage:
    return ChatResponseMessage(
        request_id=request_id or uuid4(), user_id=user_id, status=status,
        answer=make_answer(answer=text), prompt_version="v2", model="fake",
    )


@pytest.fixture(autouse=True)
def clean_hub():
    hub.reset()
    yield
    hub.reset()


# --- hub ---------------------------------------------------------------

def test_publish_toi_moi_ket_noi_cua_user():
    """1 user có thể mở nhiều tab; tab nào cũng phải nhận được câu trả lời."""
    q1, q2 = hub.subscribe(1), hub.subscribe(1)
    other = hub.subscribe(2)

    assert hub.publish(response(user_id=1)) == 2
    assert q1.qsize() == 1 and q2.qsize() == 1
    assert other.qsize() == 0


def test_publish_khi_user_khong_online_tra_ve_0():
    assert hub.publish(response(user_id=99)) == 0


def test_unsubscribe_don_sach_de_dict_khong_phinh():
    q = hub.subscribe(5)
    hub.unsubscribe(5, q)
    assert hub.connection_count(5) == 0
    assert 5 not in hub._subscribers


def test_hang_doi_day_thi_bo_event_cu_nhat():
    """Client chậm không được phép làm phình bộ nhớ vô hạn. Event bị bỏ vẫn lấy
    lại được bằng replay khi reconnect."""
    q = hub.subscribe(7)
    for _ in range(hub.QUEUE_MAX_SIZE + 5):
        hub.publish(response(user_id=7))
    assert q.qsize() == hub.QUEUE_MAX_SIZE


# --- replay buffer -----------------------------------------------------

async def test_replay_tra_ve_dung_phan_sau_moc(redis):
    first, second, third = response(), response(), response()
    for r in (first, second, third):
        await replay.remember(redis, r)

    missed = await replay.missed_since(redis, 1, first.request_id)
    assert [m.request_id for m in missed] == [second.request_id, third.request_id]


async def test_khong_gui_last_request_id_thi_khong_phat_lai(redis):
    await replay.remember(redis, response())
    assert await replay.missed_since(redis, 1, None) == []


async def test_last_request_id_la_moi_nhat_thi_khong_co_gi_de_phat_lai(redis):
    r = response()
    await replay.remember(redis, r)
    assert await replay.missed_since(redis, 1, r.request_id) == []


async def test_last_request_id_khong_tim_thay_thi_phat_lai_toan_bo(redis):
    """Client offline quá lâu: thà gửi thừa còn hơn để mất câu trả lời, FE
    dedup được bằng request_id."""
    await replay.remember(redis, response())
    await replay.remember(redis, response())
    missed = await replay.missed_since(redis, 1, uuid4())
    assert len(missed) == 2


async def test_khong_luu_event_dang_xu_ly(redis):
    """Phát lại một thông báo 'đang xử lý' đã cũ chỉ làm client hiểu nhầm."""
    await replay.remember(redis, response(status="processing"))
    assert await replay.missed_since(redis, 1, uuid4()) == []


async def test_buffer_bi_cat_theo_gioi_han(redis, monkeypatch):
    monkeypatch.setattr(settings, "sse_replay_buffer_size", 3)
    for _ in range(10):
        await replay.remember(redis, response())
    assert len(await replay.missed_since(redis, 1, uuid4())) == 3


async def test_buffer_co_ttl(redis):
    await replay.remember(redis, response(user_id=3))
    assert 0 < await redis.ttl("stream:3") <= settings.sse_replay_ttl_seconds


# --- định dạng khung SSE ----------------------------------------------

def test_khung_sse_dung_dinh_dang():
    r = response(text="nghỉ ngơi nhé")
    frame = format_event(r)
    assert frame.startswith(f"id: {r.request_id}\n")
    assert "event: ok\n" in frame
    assert frame.endswith("\n\n")
    data = json.loads(frame.split("data: ", 1)[1].strip())
    assert data["answer"]["answer"] == "nghỉ ngơi nhé"


# --- endpoint ----------------------------------------------------------
#
# Test gọi thẳng `event_stream` chứ không qua httpx: ASGITransport gom TOÀN BỘ
# body rồi mới trả về, nên một stream SSE không có điểm kết thúc sẽ treo mãi ở
# đó. Muốn test qua HTTP thật thì phải chạy uvicorn — quá nặng cho unit test.


class StubRequest:
    """Request giả, ngắt kết nối sau `disconnect_after` vòng lặp."""

    def __init__(self, disconnect_after: int = 0):
        self.disconnect_after = disconnect_after
        self.checks = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks > self.disconnect_after


async def test_reconnect_nhan_lai_cau_tra_loi_da_lo(redis):
    """Mất mạng giữa chừng là chuyện thường trên mobile: message Kafka đã được
    consume và commit, không ai phát lại nữa nếu không có bộ đệm này."""
    set_redis(redis)
    try:
        seen, missed = response(), response(text="câu trả lời bị lỡ")
        await replay.remember(redis, seen)
        await replay.remember(redis, missed)

        frames = [
            frame
            async for frame in event_stream(StubRequest(0), 1, seen.request_id)
        ]
    finally:
        set_redis(None)

    assert len(frames) == 1
    assert "câu trả lời bị lỡ" in frames[0]
    assert frames[0].startswith(f"id: {missed.request_id}")


async def test_khong_co_gi_de_phat_lai_thi_khong_gui_khung_nao(redis):
    set_redis(redis)
    try:
        frames = [f async for f in event_stream(StubRequest(0), 1, None)]
    finally:
        set_redis(None)
    assert frames == []


async def test_nhan_duoc_event_phat_ra_khi_dang_ket_noi(redis):
    set_redis(redis)
    gen = event_stream(StubRequest(disconnect_after=5), 1, None)
    try:
        task = asyncio.create_task(gen.__anext__())
        for _ in range(100):  # đợi generator kịp subscribe
            if hub.connection_count(1):
                break
            await asyncio.sleep(0.01)

        hub.publish(response(user_id=1, text="trả lời realtime"))
        frame = await asyncio.wait_for(task, timeout=2)
    finally:
        await gen.aclose()
        set_redis(None)

    assert "trả lời realtime" in frame
    assert "event: ok" in frame


async def test_gui_keepalive_khi_khong_co_event(redis, monkeypatch):
    """Proxy/LB thường đóng kết nối idle sau 30-60s."""
    monkeypatch.setattr(settings, "sse_heartbeat_seconds", 0.05)
    set_redis(redis)
    gen = event_stream(StubRequest(disconnect_after=5), 1, None)
    try:
        frame = await asyncio.wait_for(gen.__anext__(), timeout=2)
    finally:
        await gen.aclose()
        set_redis(None)

    assert frame == ": keepalive\n\n"


async def test_dong_ket_noi_thi_don_sach_subscriber(redis):
    set_redis(redis)
    gen = event_stream(StubRequest(disconnect_after=5), 42, None)
    try:
        task = asyncio.create_task(gen.__anext__())
        for _ in range(100):
            if hub.connection_count(42):
                break
            await asyncio.sleep(0.01)
        assert hub.connection_count(42) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task  # đợi generator dừng hẳn rồi mới đóng được
    finally:
        await gen.aclose()
        set_redis(None)

    assert hub.connection_count(42) == 0


async def test_response_co_header_chong_buffer_cua_nginx():
    """Thiếu X-Accel-Buffering, nginx sẽ gom event lại rồi mới đẩy một lượt và
    SSE mất hết tính realtime."""
    from app.auth import Principal

    result = await stream(
        StubRequest(0), last_request_id=None, principal=Principal(user_id=1, tier="free")
    )
    assert result.media_type == "text/event-stream"
    assert result.headers["x-accel-buffering"] == "no"
    assert result.headers["cache-control"] == "no-cache"
