from uuid import uuid4

from app.services import idempotency


async def test_consumer_guard_chi_cho_xu_ly_1_lan(redis):
    """Kafka giao lại message sau khi consumer crash trước lúc commit offset ->
    lần thứ 2 phải bị guard chặn để Phase 3 không gọi OpenAI 2 lần."""
    rid = uuid4()
    assert await idempotency.mark_processing(redis, rid) is True
    assert await idempotency.mark_processing(redis, rid) is False


async def test_unmark_cho_phep_retry_khi_xu_ly_loi(redis):
    rid = uuid4()
    await idempotency.mark_processing(redis, rid)
    await idempotency.unmark_processing(redis, rid)
    assert await idempotency.mark_processing(redis, rid) is True


async def test_guard_consumer_doc_lap_voi_claim_o_api(redis):
    """claim() (tầng API) và mark_processing() (tầng consumer) phải dùng key
    khác nhau, nếu trùng thì message vừa enqueue sẽ bị chính consumer bỏ qua."""
    rid = uuid4()
    assert await idempotency.claim(redis, rid) is True
    assert await idempotency.mark_processing(redis, rid) is True
