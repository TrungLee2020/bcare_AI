from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from app.db import documents_repository as repo
from app.db.session import db_session


async def make(user="u1", conversation_id=None, sha=None):
    document_id = uuid4()
    async with db_session() as db:
        await repo.create_document(
            db, document_id=document_id, user_id=user, conversation_id=conversation_id,
            profile_id=None, storage_path=f"{user}/{document_id}.jpg", sha256=sha or document_id.hex * 2,
        )
    return document_id


async def test_create_then_get_only_for_owner(db_maker):
    document_id = await make()
    async with db_session() as db:
        doc = await repo.get_document(db, document_id, "u1")
        assert doc.status == "received"
        assert await repo.get_document(db, document_id, "u2") is None


async def test_find_by_hash_ignores_deleted(db_maker):
    document_id = await make(sha="a" * 64)
    async with db_session() as db:
        assert (await repo.find_by_hash(db, "u1", "a" * 64)).id == document_id
        await repo.soft_delete_document(db, document_id, "u1")
    async with db_session() as db:
        assert await repo.find_by_hash(db, "u1", "a" * 64) is None
    # Đã xoá thì upload lại đúng ảnh đó được (unique index chỉ tính bản chưa xoá)
    await make(sha="a" * 64)


async def test_reopen_retries_failed_but_not_done(db_maker):
    conversation = uuid4()
    failed, done, stuck = await make(), await make(), await make()
    async with db_session() as db:
        await repo.set_status(db, failed, "quota_exceeded")
        await repo.set_status(db, done, "done")
        await repo.set_status(db, stuck, "processing")
    async with db_session() as db:
        for document_id in (failed, done, stuck):
            await repo.reopen(db, await repo.get_document(db, document_id), conversation)
    async with db_session() as db:
        assert (await repo.get_document(db, failed)).status == "received"
        assert (await repo.get_document(db, stuck)).status == "received"
        assert (await repo.get_document(db, done)).status == "done"
        assert (await repo.get_document(db, done)).conversation_id == conversation


async def test_set_status_merges_meta_and_stamps_final(db_maker):
    document_id = await make()
    async with db_session() as db:
        await repo.set_status(db, document_id, "processing", meta={"a": 1})
        await repo.set_status(db, document_id, "skipped", doc_type="not_medical", confidence=0.9, meta={"b": 2})
    async with db_session() as db:
        doc = await repo.get_document(db, document_id)
        assert doc.meta == {"a": 1, "b": 2}
        assert doc.doc_type == "not_medical"
        assert doc.processed_at is not None


async def test_save_results_keeps_extraction_history(db_maker):
    document_id = await make()
    for version in ("1", "2"):
        async with db_session() as db:
            await repo.save_results(
                db, document_id, ocr_text=f"text {version}", extraction={"v": version},
                schema_version=version, model="m", title="Đơn thuốc", doc_date=date(2026, 11, 3),
                doc_type="prescription", confidence=0.9, meta={},
            )
    async with db_session() as db:
        assert (await repo.get_ocr(db, document_id)).text == "text 2"
        assert (await repo.latest_extraction(db, document_id)).data == {"v": "2"}
        doc = await repo.get_document(db, document_id)
        assert (doc.status, doc.title, doc.doc_date) == ("done", "Đơn thuốc", date(2026, 11, 3))


async def test_list_documents_newest_first_with_cursor_and_type(db_maker):
    first, second = await make(), await make()
    async with db_session() as db:
        # Đặt thời điểm tường minh: hai bản ghi tạo liền nhau có thể trùng giờ.
        older = await repo.get_document(db, first)
        (await repo.get_document(db, second)).created_at = older.created_at + timedelta(seconds=1)
        await repo.set_status(db, first, "done", doc_type="lab_result")
    async with db_session() as db:
        page = await repo.list_documents(db, "u1", 1)
        assert [d.id for d in page] == [second]
        older = await repo.list_documents(db, "u1", 10, before=page[0].created_at)
        assert [d.id for d in older] == [first]
        assert [d.id for d in await repo.list_documents(db, "u1", 10, doc_type="lab_result")] == [first]


async def test_conversation_documents_returns_latest_extraction(db_maker):
    conversation = uuid4()
    document_id = await make(conversation_id=conversation)
    await make(conversation_id=uuid4())
    async with db_session() as db:
        await repo.save_results(
            db, document_id, ocr_text="t", extraction={"k": 1}, schema_version="1", model="m",
            title="x", doc_date=None, doc_type="prescription", confidence=0.9, meta={},
        )
    async with db_session() as db:
        rows = await repo.conversation_documents(db, conversation, "u1", 3)
    assert [(d.id, data) for d, data in rows] == [(document_id, {"k": 1})]


async def test_purge_removes_old_deleted_documents_and_results(db_maker):
    document_id = await make()
    async with db_session() as db:
        await repo.save_results(
            db, document_id, ocr_text="t", extraction={}, schema_version="1", model="m",
            title="x", doc_date=None, doc_type="prescription", confidence=0.9, meta={},
        )
        await repo.soft_delete_document(db, document_id, "u1")
    async with db_session() as db:
        purged = await repo.purge_deleted_documents(db, datetime.now(timezone.utc) + timedelta(seconds=1))
    assert purged == 1
    async with db_session() as db:
        assert await repo.get_ocr(db, document_id) is None
