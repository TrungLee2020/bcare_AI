import io
from uuid import uuid4

import pytest
from PIL import Image

from app.config import settings
from app.db import documents_repository as repo
from app.db.session import db_session
from app.services import openai_client, quota
from app.services.documents import pipeline
from tests.fake_vision import FakeVision, make_classification


def jpeg_bytes() -> bytes:
    img = Image.new("RGB", (1200, 900), (90, 90, 90))
    img.paste((255, 255, 255), (200, 100, 1000, 800))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture
def vision():
    fake = FakeVision()
    openai_client.set_openai(fake)
    yield fake
    openai_client.set_openai(None)


async def new_document(redis, user="u1", stash=True):
    document_id = uuid4()
    async with db_session() as db:
        await repo.create_document(
            db, document_id=document_id, user_id=user, conversation_id=None, profile_id=None,
            storage_path=f"{user}/{document_id}.jpg", sha256=document_id.hex * 2,
        )
    if stash:
        await pipeline.stash_image(redis, document_id, jpeg_bytes())
    return document_id


async def load(document_id):
    async with db_session() as db:
        doc = await repo.get_document(db, document_id)
        return doc, await repo.get_ocr(db, document_id), await repo.latest_extraction(db, document_id)


async def ocr_used(redis, user="u1") -> int:
    return int(await redis.get(quota.daily_key(pipeline.OCR_QUOTA_PREFIX, user)) or 0)


async def test_prescription_is_classified_read_and_saved(db_maker, redis, vision):
    document_id = await new_document(redis)
    assert await pipeline.process(redis, document_id) == "done"
    doc, ocr, extraction = await load(document_id)
    assert doc.status == "done"
    assert doc.doc_type == "prescription"
    assert doc.title == "Đơn thuốc – BV Chợ Rẫy – 03/11/2026"
    assert str(doc.doc_date) == "2026-11-03"
    assert ocr.text.startswith("BỆNH VIỆN CHỢ RẪY")
    assert "ocr_text" not in extraction.data
    assert extraction.data["medications"][0]["name"] == "Paracetamol"
    assert vision.names() == ["document_classification", "document_extraction"]
    assert vision.calls[0]["messages"][1]["content"][1]["image_url"]["detail"] == "low"
    assert vision.calls[1]["messages"][1]["content"][1]["image_url"]["detail"] == "high"
    assert await ocr_used(redis) == 1
    assert await redis.get(pipeline.image_key(document_id)) is None
    assert {"classify", "extract"} <= set(doc.meta)


@pytest.mark.parametrize(
    "classification, status",
    [
        (make_classification(doc_type="not_medical"), "skipped"),
        (make_classification(confidence=0.3), "skipped"),
        (make_classification(readable=False), "unreadable"),
    ],
)
async def test_stops_before_ocr_without_using_quota(db_maker, redis, vision, classification, status):
    vision.classification = classification
    document_id = await new_document(redis)
    assert await pipeline.process(redis, document_id) == status
    assert vision.names() == ["document_classification"]
    assert await ocr_used(redis) == 0


async def test_imaging_film_is_stored_but_not_read(db_maker, redis, vision):
    vision.classification = make_classification(doc_type="imaging_film")
    document_id = await new_document(redis)
    assert await pipeline.process(redis, document_id) == "done"
    doc, ocr, extraction = await load(document_id)
    assert (doc.title, ocr, extraction) == ("Phim chụp", None, None)
    assert vision.names() == ["document_classification"]
    assert await ocr_used(redis) == 0


async def test_daily_limit(db_maker, redis, vision, monkeypatch):
    monkeypatch.setattr(settings, "ocr_max_per_day", 1)
    first, second = await new_document(redis), await new_document(redis)
    assert await pipeline.process(redis, first) == "done"
    assert await pipeline.process(redis, second) == "quota_exceeded"
    assert vision.names().count("document_extraction") == 1


async def test_extraction_error_refunds_quota(db_maker, redis, vision, monkeypatch):
    vision.extract_error = RuntimeError("model down")
    document_id = await new_document(redis)
    assert await pipeline.process(redis, document_id) == "failed"
    doc, _, _ = await load(document_id)
    assert doc.meta["reason"] == "model_error"
    assert await ocr_used(redis) == 0


async def test_expired_image_fails_without_model_call(db_maker, redis, vision):
    document_id = await new_document(redis, stash=False)
    assert await pipeline.process(redis, document_id) == "failed"
    doc, _, _ = await load(document_id)
    assert doc.meta["reason"] == "image_expired"
    assert vision.calls == []


async def test_finished_document_is_not_processed_again(db_maker, redis, vision):
    document_id = await new_document(redis)
    await pipeline.process(redis, document_id)
    vision.calls.clear()
    assert await pipeline.process(redis, document_id) == "done"
    assert vision.calls == []


async def test_missing_document(db_maker, redis, vision):
    assert await pipeline.process(redis, uuid4()) == "missing"


def test_build_title_skips_missing_parts():
    assert pipeline.build_title("lab_result", None, None) == "Kết quả xét nghiệm"
