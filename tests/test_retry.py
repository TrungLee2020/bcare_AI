import asyncio

import httpx
import openai
import pytest

from app.services.retry import backoff_delay, call_with_backoff, is_retryable


def timeout_error() -> openai.APITimeoutError:
    return openai.APITimeoutError(request=httpx.Request("POST", "http://x"))


def make_flaky(fail_times: int, exc_factory=timeout_error):
    state = {"calls": 0}

    async def factory():
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise exc_factory()
        return "ok"

    return factory, state


async def test_loi_tam_thoi_duoc_thu_lai_va_thanh_cong():
    # base_delay nhỏ nên test không phải ngủ thật
    factory, state = make_flaky(2)
    result = await call_with_backoff(factory, attempts=3, base_delay=0.01, max_delay=0.1)
    assert result == "ok"
    assert state["calls"] == 3


async def test_het_so_lan_thu_thi_nem_loi():
    factory, state = make_flaky(10)
    with pytest.raises(openai.APITimeoutError):
        await call_with_backoff(factory, attempts=3, base_delay=0.01, max_delay=0.1)
    assert state["calls"] == 3


async def test_loi_vinh_vien_khong_duoc_retry():
    """Retry lỗi vĩnh viễn (sai API key, request không hợp lệ) chỉ giữ message
    trong partition lâu hơn, làm các user khác cùng partition phải chờ theo."""
    factory, state = make_flaky(10, exc_factory=lambda: ValueError("sai tham số"))
    with pytest.raises(ValueError):
        await call_with_backoff(factory, attempts=5, base_delay=0.01, max_delay=0.1)
    assert state["calls"] == 1


def test_phan_loai_dung_loai_loi():
    req = httpx.Request("POST", "http://x")
    assert is_retryable(openai.APIConnectionError(request=req))
    assert is_retryable(openai.RateLimitError("x", response=httpx.Response(429, request=req), body=None))
    assert not is_retryable(ValueError())
    assert not is_retryable(openai.AuthenticationError("x", response=httpx.Response(401, request=req), body=None))


def test_backoff_tang_dan_va_bi_chan_tran():
    for attempt in range(1, 8):
        delay = backoff_delay(attempt, base=1.0, cap=8.0)
        assert 0 < delay <= 8.0
    # lần sau nhìn chung phải lâu hơn lần đầu
    assert backoff_delay(5, 1.0, 100.0) > backoff_delay(1, 1.0, 100.0)


def test_backoff_co_jitter():
    """Không jitter thì mọi consumer cùng bị rate-limit sẽ cùng ngủ đúng bằng
    nhau rồi cùng thức dậy đập vào API một lượt."""
    delays = {backoff_delay(3, 1.0, 100.0) for _ in range(50)}
    assert len(delays) > 1
