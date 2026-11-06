"""
Xử lý một tài liệu (spec mục 3.3): phân loại → nếu là giấy tờ y tế đọc được
thì OCR + trích xuất → lưu.

Idempotent: tài liệu đã ở trạng thái cuối thì không làm lại (Kafka có thể giao
lại message). Ảnh lấy từ Redis, nơi API đặt tạm lúc nhận request: lúc consumer
chạy, token Supabase của user có thể đã hết hạn.
"""

import asyncio
import base64
import binascii
import logging
from datetime import date
from uuid import UUID

from redis.asyncio import Redis

from app import metrics
from app.config import settings
from app.db import documents_repository as repo
from app.db.session import db_session
from app.redis_client import get_redis
from app.services import cost, openai_client, quota
from app.services.documents import image
from app.services.documents.schemas import SCHEMA_VERSION

logger = logging.getLogger(__name__)

OCR_QUOTA_PREFIX = "ocr_quota"

TYPE_LABELS = {
    "prescription": "Đơn thuốc",
    "lab_result": "Kết quả xét nghiệm",
    "medical_record": "Hồ sơ khám bệnh",
    "imaging_report": "Kết quả chụp chiếu",
    "imaging_film": "Phim chụp",
    "other_medical": "Giấy tờ y tế",
    "not_medical": "Ảnh",
}


def image_key(document_id: UUID) -> str:
    return f"ocr_image:{document_id}"


async def stash_image(redis: Redis, document_id: UUID, data: bytes) -> None:
    # Redis của app chạy decode_responses=True: lưu base64, không lưu bytes thô.
    await redis.set(
        image_key(document_id), base64.b64encode(data).decode("ascii"), ex=settings.ocr_image_ttl_seconds
    )


def build_title(doc_type: str, facility: str | None, doc_date: date | None) -> str:
    parts = [TYPE_LABELS.get(doc_type, "Giấy tờ y tế")]
    if facility and facility.strip():
        parts.append(facility.strip())
    if doc_date is not None:
        parts.append(doc_date.strftime("%d/%m/%Y"))
    return " – ".join(parts)


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


async def _finish(redis: Redis, document_id: UUID, status: str, *, reason: str = "", meta: dict | None = None, **fields) -> str:
    meta = dict(meta or {})
    if reason:
        meta["reason"] = reason
    async with db_session() as db:
        await repo.set_status(db, document_id, status, meta=meta, **fields)
    await redis.delete(image_key(document_id))
    metrics.incr(f"ocr_{status}")
    logger.info("Tài liệu %s → %s %s", document_id, status, reason)
    return status


async def process(redis: Redis, document_id: UUID) -> str:
    async with db_session() as db:
        doc = await repo.get_document(db, document_id)
        if doc is None:
            return "missing"
        if doc.status in repo.FINAL_STATUSES:
            return doc.status
        user_id = doc.user_id
        await repo.set_status(db, document_id, "processing")

    raw = await redis.get(image_key(document_id))
    if raw is None:
        return await _finish(redis, document_id, "failed", reason="image_expired")
    try:
        data = base64.b64decode(raw)
        small = image.prepare(data, settings.ocr_classify_side, settings.ocr_max_crop_ratio)
    except (image.InvalidImage, binascii.Error):
        return await _finish(redis, document_id, "failed", reason="invalid_image")

    try:
        found, usage = await asyncio.wait_for(
            openai_client.classify_document(small.data_url), settings.ocr_llm_budget_seconds
        )
    except Exception as exc:
        logger.warning("Phân loại tài liệu %s lỗi: %s", document_id, type(exc).__name__)
        return await _finish(redis, document_id, "failed", reason="model_error")

    meta = {
        "classify": cost.record(usage, settings.ocr_classify_model),
        "classify_prompt": settings.ocr_classify_prompt_version,
    }
    metrics.incr(f"ocr_classified_{found.doc_type}")
    common = dict(doc_type=found.doc_type, confidence=found.confidence)

    if found.doc_type == "not_medical" or found.confidence < settings.ocr_min_confidence:
        return await _finish(redis, document_id, "skipped", meta=meta, **common)
    if not found.readable:
        return await _finish(redis, document_id, "unreadable", meta=meta, **common)
    if found.doc_type == "imaging_film":
        # Phim chụp: chỉ lưu, không đọc, không diễn giải (chẩn đoán hình ảnh).
        return await _finish(redis, document_id, "done", meta=meta, title=build_title("imaging_film", None, None), **common)

    if not await quota.consume_daily(redis, OCR_QUOTA_PREFIX, user_id, settings.ocr_max_per_day):
        return await _finish(redis, document_id, "quota_exceeded", meta=meta, **common)

    model = openai_client.extract_model()
    try:
        large = image.prepare(data, settings.ocr_max_side, settings.ocr_max_crop_ratio)
        extraction, usage = await asyncio.wait_for(
            openai_client.extract_document(large.data_url, found.doc_type), settings.ocr_llm_budget_seconds
        )
    except asyncio.CancelledError:
        # Bị huỷ (hết ngân sách của tin / tắt máy) sau khi đã trừ lượt: hoàn lại.
        await quota.refund_daily(redis, OCR_QUOTA_PREFIX, user_id)
        raise
    except Exception as exc:
        await quota.refund_daily(redis, OCR_QUOTA_PREFIX, user_id)
        logger.warning("Đọc tài liệu %s lỗi: %s", document_id, type(exc).__name__)
        return await _finish(redis, document_id, "failed", reason="model_error", meta=meta, **common)

    meta["extract"] = cost.record(usage, model)
    meta["extract_prompt"] = settings.ocr_extract_prompt_version
    meta["image"] = {"width": large.width, "height": large.height}
    data_dict = extraction.model_dump()
    ocr_text = data_dict.pop("ocr_text")
    doc_date = _parse_date(data_dict.get("doc_date"))
    try:
        async with db_session() as db:
            await repo.save_results(
                db,
                document_id,
                ocr_text=ocr_text,
                extraction=data_dict,
                schema_version=SCHEMA_VERSION,
                model=model,
                title=build_title(found.doc_type, data_dict.get("facility"), doc_date),
                doc_date=doc_date,
                doc_type=found.doc_type,
                confidence=found.confidence,
                meta=meta,
            )
    except Exception:
        # Lưu lỗi thì chưa có kết quả: hoàn lượt để lần giao lại không bị trừ hai lần.
        await quota.refund_daily(redis, OCR_QUOTA_PREFIX, user_id)
        raise
    except asyncio.CancelledError:
        await quota.refund_daily(redis, OCR_QUOTA_PREFIX, user_id)
        raise
    await redis.delete(image_key(document_id))
    metrics.incr("ocr_done")
    logger.info("Tài liệu %s → done (%s)", document_id, found.doc_type)
    return "done"


async def fail_unfinished(document_ids: list[UUID], reason: str) -> None:
    """Đánh failed các ảnh chưa ở trạng thái cuối và xoá ảnh tạm. Best effort:
    chạy ở đường xử lý lỗi nên không được ném."""
    for document_id in document_ids:
        try:
            async with db_session() as db:
                doc = await repo.get_document(db, document_id)
                if doc is None or doc.status in repo.FINAL_STATUSES:
                    continue
                await repo.set_status(db, document_id, "failed", meta={"reason": reason})
            await get_redis().delete(image_key(document_id))
            metrics.incr("ocr_failed")
        except Exception as exc:
            logger.warning("Không đánh failed được tài liệu %s: %s", document_id, type(exc).__name__)


async def process_all(redis: Redis, document_ids: list[UUID]) -> None:
    """Đọc mọi ảnh của một tin song song, trong tổng ngân sách thời gian. Không
    bao giờ ném: ảnh lỗi thì hiện ra là chưa đọc được trong ngữ cảnh, câu hỏi
    vẫn được trả lời."""
    if not document_ids:
        return
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*(process(redis, d) for d in document_ids), return_exceptions=True),
            settings.ocr_message_budget_seconds,
        )
    except asyncio.TimeoutError:
        logger.warning("Đọc ảnh quá ngân sách %.0fs", settings.ocr_message_budget_seconds)
        metrics.incr("ocr_message_budget_exceeded")
        await fail_unfinished(document_ids, "message_budget")
        return
    for document_id, result in zip(document_ids, results):
        if isinstance(result, BaseException):
            logger.warning("Đọc tài liệu %s lỗi: %s", document_id, type(result).__name__)
    # Lỗi hạ tầng (vd DB) làm ảnh kẹt ở processing/received: đánh failed để không treo mãi.
    if any(isinstance(r, BaseException) for r in results):
        await fail_unfinished(document_ids, "process_error")
