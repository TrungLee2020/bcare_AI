"""Truy vấn bảng tài liệu y tế: health_documents, OCR, bản trích xuất."""

from datetime import date, datetime, timezone
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HealthDocument, HealthDocumentExtraction, HealthDocumentOcr

FINAL_STATUSES = frozenset({"done", "skipped", "unreadable", "quota_exceeded", "failed"})
# Gửi lại đúng ảnh này thì đọc lại: chụp lại rõ hơn, hoặc hôm sau còn hạn mức.
# Cả "processing" (worker chết giữa chừng): an toàn vì Kafka chia partition theo
# user_id nên tin của một user chạy tuần tự, gửi lại không đua với lần chạy cũ.
RETRYABLE_STATUSES = frozenset({"processing", "unreadable", "quota_exceeded", "failed"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create_document(
    db: AsyncSession,
    *,
    document_id: UUID,
    user_id: str,
    conversation_id: UUID | None,
    profile_id: str | None,
    storage_path: str,
    sha256: str,
) -> HealthDocument:
    doc = HealthDocument(
        id=document_id,
        user_id=user_id,
        conversation_id=conversation_id,
        profile_id=profile_id,
        storage_path=storage_path,
        sha256=sha256,
        status="received",
        meta={},
        created_at=_now(),
    )
    db.add(doc)
    await db.flush()
    return doc


async def find_by_hash(db: AsyncSession, user_id: str, sha256: str) -> HealthDocument | None:
    stmt = select(HealthDocument).where(
        HealthDocument.user_id == user_id,
        HealthDocument.sha256 == sha256,
        HealthDocument.deleted_at.is_(None),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_document(db: AsyncSession, document_id: UUID, user_id: str | None = None) -> HealthDocument | None:
    stmt = select(HealthDocument).where(HealthDocument.id == document_id, HealthDocument.deleted_at.is_(None))
    if user_id is not None:
        stmt = stmt.where(HealthDocument.user_id == user_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def reopen(db: AsyncSession, doc: HealthDocument, conversation_id: UUID | None) -> None:
    """Gửi lại ảnh đã có: gắn sang hội thoại hiện tại; trạng thái đọc lỗi thì
    đặt lại để đọc lại."""
    doc.conversation_id = conversation_id
    if doc.status in RETRYABLE_STATUSES:
        doc.status = "received"
        doc.processed_at = None


async def set_status(
    db: AsyncSession,
    document_id: UUID,
    status: str,
    *,
    doc_type: str | None = None,
    confidence: float | None = None,
    title: str | None = None,
    meta: dict | None = None,
) -> None:
    doc = await db.get(HealthDocument, document_id)
    doc.status = status
    if doc_type is not None:
        doc.doc_type = doc_type
    if confidence is not None:
        doc.confidence = confidence
    if title is not None:
        doc.title = title
    if meta:
        # Gán dict mới: sửa tại chỗ thì SQLAlchemy không thấy thay đổi của cột JSON.
        doc.meta = {**(doc.meta or {}), **meta}
    if status in FINAL_STATUSES:
        doc.processed_at = _now()


async def save_results(
    db: AsyncSession,
    document_id: UUID,
    *,
    ocr_text: str,
    extraction: dict,
    schema_version: str,
    model: str,
    title: str,
    doc_date: date | None,
    doc_type: str,
    confidence: float,
    meta: dict,
) -> None:
    await db.execute(delete(HealthDocumentOcr).where(HealthDocumentOcr.document_id == document_id))
    db.add(HealthDocumentOcr(document_id=document_id, text=ocr_text, model=model))
    db.add(
        HealthDocumentExtraction(
            document_id=document_id, schema_version=schema_version, data=extraction, model=model
        )
    )
    doc = await db.get(HealthDocument, document_id)
    doc.doc_date = doc_date
    await set_status(
        db, document_id, "done", doc_type=doc_type, confidence=confidence, title=title, meta=meta
    )


async def get_ocr(db: AsyncSession, document_id: UUID) -> HealthDocumentOcr | None:
    return await db.get(HealthDocumentOcr, document_id)


async def latest_extraction(db: AsyncSession, document_id: UUID) -> HealthDocumentExtraction | None:
    stmt = (
        select(HealthDocumentExtraction)
        .where(HealthDocumentExtraction.document_id == document_id)
        .order_by(HealthDocumentExtraction.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_documents(
    db: AsyncSession,
    user_id: str,
    limit: int,
    before: datetime | None = None,
    doc_type: str | None = None,
) -> list[HealthDocument]:
    stmt = select(HealthDocument).where(HealthDocument.user_id == user_id, HealthDocument.deleted_at.is_(None))
    if before is not None:
        stmt = stmt.where(HealthDocument.created_at < before)
    if doc_type is not None:
        stmt = stmt.where(HealthDocument.doc_type == doc_type)
    stmt = stmt.order_by(HealthDocument.created_at.desc(), HealthDocument.id.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars())


async def conversation_documents(
    db: AsyncSession, conversation_id: UUID, user_id: str, limit: int
) -> list[tuple[HealthDocument, dict | None]]:
    """Tài liệu gần nhất gắn với hội thoại, kèm bản trích xuất mới nhất (None
    nếu chưa đọc xong / không đọc)."""
    stmt = (
        select(HealthDocument)
        .where(
            HealthDocument.conversation_id == conversation_id,
            HealthDocument.user_id == user_id,
            HealthDocument.deleted_at.is_(None),
        )
        .order_by(HealthDocument.created_at.desc())
        .limit(limit)
    )
    rows = []
    for doc in (await db.execute(stmt)).scalars():
        extraction = await latest_extraction(db, doc.id) if doc.status == "done" else None
        rows.append((doc, extraction.data if extraction else None))
    return rows


async def soft_delete_document(db: AsyncSession, document_id: UUID, user_id: str) -> HealthDocument | None:
    doc = await get_document(db, document_id, user_id)
    if doc is not None:
        doc.deleted_at = _now()
    return doc


async def purge_deleted_documents(db: AsyncSession, deleted_before: datetime) -> int:
    """Xoá hẳn tài liệu đã ẩn trước `deleted_before`. Xoá OCR / bản trích xuất
    tường minh: SQLite (test) chỉ cascade khi bật PRAGMA foreign_keys."""
    expired = select(HealthDocument.id).where(HealthDocument.deleted_at < deleted_before)
    await db.execute(delete(HealthDocumentOcr).where(HealthDocumentOcr.document_id.in_(expired)))
    await db.execute(delete(HealthDocumentExtraction).where(HealthDocumentExtraction.document_id.in_(expired)))
    result = await db.execute(delete(HealthDocument).where(HealthDocument.deleted_at < deleted_before))
    return result.rowcount
