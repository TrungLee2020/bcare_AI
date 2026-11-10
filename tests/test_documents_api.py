import io

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from PIL import Image

from app.api import v1
from app.config import settings
from app.db import documents_repository as repo
from app.db import repository
from app.db.session import db_session
from app.redis_client import set_redis
from app.services import consent, quota
from app.services.consent import Consent
from app.services.documents import pipeline, storage
from app.sse import hub
from tests.test_v1_api import ANSWER, JWT_SECRET, SUPABASE_URL, USER, FakePipeline, auth, chat_body

JSON = {"Accept": "application/json"}


def jpeg(color=(255, 255, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), color).save(buf, "JPEG")
    return buf.getvalue()


class FakeStorage:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def download(self, path, token):
        if path not in self.objects:
            raise storage.ObjectNotFound(path)
        return self.objects[path]

    async def delete(self, path, token):
        self.deleted.append(path)


@pytest.fixture(autouse=True)
def api_settings(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", SUPABASE_URL)
    monkeypatch.setattr(settings, "supabase_jwt_secret", JWT_SECRET)
    monkeypatch.setattr(settings, "consent_required", False)
    monkeypatch.setattr(settings, "v1_answer_timeout_seconds", 3.0)
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(v1, "DELTA_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(v1, "POLL_SECONDS", 0.05)
    hub.reset()
    yield
    hub.reset()


@pytest.fixture
def files(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setattr(storage, "download", fake.download)
    monkeypatch.setattr(storage, "delete", fake.delete)
    return fake


@pytest.fixture
def kafka(monkeypatch, redis):
    fake = FakePipeline(redis)
    monkeypatch.setattr("app.api.v1.publish_chat_request", fake)
    return fake


@pytest_asyncio.fixture
async def client(redis, db_maker):
    from fastapi import FastAPI

    set_redis(redis)
    app = FastAPI()
    v1.install(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    set_redis(None)


def with_images(*paths, text=""):
    return chat_body(text, attachments=[{"path": p} for p in paths])


async def test_image_only_is_acknowledged_without_using_chat_quota(client, files, kafka, redis):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    resp = await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers={**auth(), **JSON})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"].startswith("Bụt đã nhận 1 ảnh")
    [document] = body["documents"]
    assert document["status"] == "received"
    [published] = kafka.published
    assert published.content == ""
    assert [str(d) for d in published.document_ids] == [document["id"]]
    assert await quota.remaining(redis, USER, "free") == settings.quota_free_per_day
    assert await redis.get(pipeline.image_key(published.document_ids[0])) is not None


async def test_image_only_sse_done_event_has_documents(client, files, kafka):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    resp = await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers=auth())
    done = [line for line in resp.text.splitlines() if '"type": "done"' in line]
    assert done and '"documents"' in done[0]


async def test_question_with_image_goes_through_chat_pipeline(client, files, kafka, redis):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    resp = await client.post(
        "/v1/chat", json=with_images(f"{USER}/a.jpg", text="đơn này có mấy thuốc"), headers={**auth(), **JSON}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply"] == ANSWER
    assert len(body["documents"]) == 1
    [published] = kafka.published
    assert published.content == "đơn này có mấy thuốc"
    assert len(published.document_ids) == 1
    assert await quota.remaining(redis, USER, "free") == settings.quota_free_per_day - 1


async def test_same_image_twice_reuses_document_and_deletes_duplicate_upload(client, files, kafka):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    files.objects[f"{USER}/b.jpg"] = jpeg()
    first = (await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers={**auth(), **JSON})).json()
    second = (await client.post("/v1/chat", json=with_images(f"{USER}/b.jpg"), headers={**auth(), **JSON})).json()
    assert first["documents"][0]["id"] == second["documents"][0]["id"]
    assert files.deleted == [f"{USER}/b.jpg"]


@pytest.mark.parametrize(
    "paths, status, code",
    [
        (["someone-else/a.jpg"], 403, "forbidden"),
        ([f"{USER}/missing.jpg"], 422, "invalid_attachment"),
        ([f"{USER}/a.jpg"] * 4, 422, "invalid_request"),
    ],
)
async def test_rejected_attachments(client, files, kafka, paths, status, code):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    resp = await client.post("/v1/chat", json=with_images(*paths), headers={**auth(), **JSON})
    assert (resp.status_code, resp.json()["error"]) == (status, code)
    assert kafka.published == []


async def test_not_an_image(client, files, kafka):
    files.objects[f"{USER}/a.jpg"] = b"%PDF-1.4"
    resp = await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers={**auth(), **JSON})
    assert (resp.status_code, resp.json()["error"]) == (422, "invalid_attachment")


async def test_disabled(client, files, kafka, monkeypatch):
    monkeypatch.setattr(settings, "ocr_enabled", False)
    resp = await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers={**auth(), **JSON})
    assert (resp.status_code, resp.json()["error"]) == (403, "not_enabled")


async def test_requires_share_profile_consent(client, files, kafka, monkeypatch):
    async def no_share(redis, user_id, token=""):
        return Consent(ai_chat=True, share_profile=False)

    monkeypatch.setattr(consent, "check", no_share)
    resp = await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers={**auth(), **JSON})
    assert (resp.status_code, resp.json()["error"]) == (403, "consent_required")


async def test_text_only_chat_is_unchanged(client, kafka):
    resp = await client.post("/v1/chat", json=chat_body(), headers={**auth(), **JSON})
    assert resp.status_code == 200
    assert "documents" not in resp.json()


async def test_documents_list_detail_delete(client, files, kafka):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    sent = (await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers={**auth(), **JSON})).json()
    document_id = sent["documents"][0]["id"]
    async with db_session() as db:
        await repo.save_results(
            db, kafka.published[0].document_ids[0], ocr_text="ĐƠN THUỐC", extraction={"uncertain_fields": ["doctor"]},
            schema_version="1", model="m", title="Đơn thuốc", doc_date=None, doc_type="prescription",
            confidence=0.9, meta={},
        )

    listed = (await client.get("/v1/documents", headers=auth())).json()
    assert [d["id"] for d in listed["documents"]] == [document_id]
    assert listed["documents"][0]["title"] == "Đơn thuốc"
    assert listed["next_cursor"] is None

    detail = (await client.get(f"/v1/documents/{document_id}", headers=auth())).json()
    assert detail["ocr_text"] == "ĐƠN THUỐC"
    assert detail["uncertain_fields"] == ["doctor"]

    other = await client.get(f"/v1/documents/{document_id}", headers=auth(sub="11111111-2222-4333-8444-555555555555"))
    assert other.status_code == 404

    assert (await client.delete(f"/v1/documents/{document_id}", headers=auth())).status_code == 204
    assert files.deleted == [f"{USER}/a.jpg"]
    assert (await client.get("/v1/documents", headers=auth())).json()["documents"] == []
    assert (await client.get(f"/v1/documents/{document_id}", headers=auth())).status_code == 404


async def test_documents_rejects_bad_type_and_cursor(client):
    assert (await client.get("/v1/documents?type=abc", headers=auth())).status_code == 422
    assert (await client.get("/v1/documents?cursor=yesterday", headers=auth())).status_code == 422


async def test_conversation_started_by_image_has_a_title(client, db_maker):
    from uuid import uuid4

    session_id = uuid4()
    async with db_session() as db:
        await repository.ensure_session(db, session_id, USER)
        await repository.save_turn(
            db, session_id=session_id, user_id=USER, request_id=uuid4(), question="",
            answer="Bụt đã nhận 1 ảnh", meta={}, user_meta={"documents": ["x"]},
        )
    listed = (await client.get("/v1/conversations", headers=auth())).json()
    assert listed["conversations"][0]["title"] == "Ảnh giấy tờ"


async def _status(document_id) -> tuple[str, dict]:
    from uuid import UUID

    async with db_session() as db:
        doc = await repo.get_document(db, UUID(str(document_id)))
        return doc.status, dict(doc.meta or {})


async def _all_documents(client):
    return (await client.get("/v1/documents", headers=auth())).json()["documents"]


async def test_quota_exceeded_fails_accepted_documents(client, files, kafka, monkeypatch):
    monkeypatch.setattr(settings, "quota_free_per_day", 0)
    files.objects[f"{USER}/a.jpg"] = jpeg()
    resp = await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg", text="đơn này sao"), headers={**auth(), **JSON})
    assert (resp.status_code, resp.json()["error"]) == (429, "quota_exceeded")
    [doc] = await _all_documents(client)
    status, meta = await _status(doc["id"])
    assert status == "failed"
    assert meta.get("reason") == "request_failed"


async def test_forbidden_second_path_leaves_no_document(client, files, kafka):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    resp = await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg", "someone-else/b.jpg"), headers={**auth(), **JSON})
    assert (resp.status_code, resp.json()["error"]) == (403, "forbidden")
    assert await _all_documents(client) == []


async def test_publish_failure_on_image_only_fails_document(client, files, kafka):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    kafka.fail = True
    resp = await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers={**auth(), **JSON})
    assert (resp.status_code, resp.json()["error"]) == (503, "unavailable")
    [doc] = await _all_documents(client)
    assert (await _status(doc["id"]))[0] == "failed"


async def test_done_duplicate_is_not_downgraded(client, files, kafka, monkeypatch):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    files.objects[f"{USER}/b.jpg"] = jpeg()
    first = (await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers={**auth(), **JSON})).json()
    document_id = first["documents"][0]["id"]
    async with db_session() as db:
        await repo.save_results(
            db, kafka.published[0].document_ids[0], ocr_text="x", extraction={}, schema_version="1", model="m",
            title="t", doc_date=None, doc_type="prescription", confidence=0.9, meta={},
        )
    monkeypatch.setattr(settings, "quota_free_per_day", 0)
    resp = await client.post("/v1/chat", json=with_images(f"{USER}/b.jpg", text="hỏi"), headers={**auth(), **JSON})
    assert (resp.status_code, resp.json()["error"]) == (429, "quota_exceeded")
    assert (await _status(document_id))[0] == "done"


OTHER_USER = "11111111-2222-4333-8444-555555555555"


async def test_idempotent_retry_does_not_reopen_documents(client, files, kafka):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    headers = {**auth(), **JSON, "Idempotency-Key": "k1"}
    first = (await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers=headers)).json()
    document_id = first["documents"][0]["id"]
    async with db_session() as db:
        await repo.set_status(db, kafka.published[0].document_ids[0], "failed", meta={"reason": "model_error"})

    again = await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers=headers)
    assert again.status_code == 200
    assert [d["id"] for d in again.json()["documents"]] == [document_id]
    assert len(kafka.published) == 1
    assert (await _status(document_id))[0] == "failed"


async def _other_users_document(path: str):
    from uuid import uuid4

    async with db_session() as db:
        doc = await repo.create_document(
            db, document_id=uuid4(), user_id=OTHER_USER, conversation_id=None, profile_id=None,
            storage_path=path, sha256="f" * 64,
        )
        return doc.id


async def test_delete_all_documents_only_touches_the_owner(client, files, kafka):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    files.objects[f"{USER}/b.jpg"] = jpeg((0, 0, 0))
    await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg", f"{USER}/b.jpg"), headers={**auth(), **JSON})
    other_id = await _other_users_document(f"{OTHER_USER}/x.jpg")
    assert len(await _all_documents(client)) == 2

    assert (await client.delete("/v1/documents", headers=auth())).status_code == 204
    assert await _all_documents(client) == []
    assert sorted(files.deleted) == [f"{USER}/a.jpg", f"{USER}/b.jpg"]
    others = (await client.get("/v1/documents", headers=auth(sub=OTHER_USER))).json()["documents"]
    assert [d["id"] for d in others] == [str(other_id)]


async def test_delete_all_documents_survives_storage_errors(client, files, kafka, monkeypatch):
    files.objects[f"{USER}/a.jpg"] = jpeg()
    await client.post("/v1/chat", json=with_images(f"{USER}/a.jpg"), headers={**auth(), **JSON})

    async def broken(path, token):
        raise storage.StorageError("HTTP 500")

    monkeypatch.setattr(storage, "delete", broken)
    assert (await client.delete("/v1/documents", headers=auth())).status_code == 204
    assert await _all_documents(client) == []


async def test_cannot_delete_another_users_document(client, files, kafka):
    other_id = await _other_users_document(f"{OTHER_USER}/x.jpg")
    resp = await client.delete(f"/v1/documents/{other_id}", headers=auth())
    assert (resp.status_code, resp.json()["error"]) == (404, "not_found")
    assert files.deleted == []
    others = (await client.get("/v1/documents", headers=auth(sub=OTHER_USER))).json()["documents"]
    assert [d["id"] for d in others] == [str(other_id)]


async def test_documents_naive_cursor_is_treated_as_utc(client, monkeypatch):
    seen = {}
    real = v1.documents_repository.list_documents

    async def spy(db, user_id, limit, before=None, doc_type=None):
        seen["before"] = before
        return await real(db, user_id, limit, before, doc_type)

    monkeypatch.setattr(v1.documents_repository, "list_documents", spy)
    resp = await client.get("/v1/documents?cursor=2026-11-07T10:00:00", headers=auth())
    assert resp.status_code == 200
    assert seen["before"].utcoffset() is not None and seen["before"].utcoffset().total_seconds() == 0
