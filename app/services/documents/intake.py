"""
Nhận một ảnh user gửi kèm tin chat (spec mục 3.2): tải từ Storage bằng token
user, kiểm là ảnh, xử lý trùng theo sha256, ghi bản ghi, đặt ảnh vào Redis
cho consumer.
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from uuid import UUID, uuid4

from redis.asyncio import Redis

from app.db import documents_repository as repo
from app.db.session import db_session
from app.services.documents import image, pipeline, storage

logger = logging.getLogger(__name__)


class AttachmentRejected(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Accepted:
    document_id: UUID
    status: str


async def accept(
    redis: Redis,
    *,
    user_id: str,
    access_token: str,
    conversation_id: UUID,
    path: str,
    profile_id: str | None,
) -> Accepted:
    if not storage.is_owned(path, user_id):
        raise AttachmentRejected(403, "forbidden", "Ảnh không thuộc tài khoản của bạn.")
    try:
        data = await storage.download(path, access_token)
    except storage.ObjectNotFound as exc:
        raise AttachmentRejected(422, "invalid_attachment", "Không tìm thấy ảnh, bạn gửi lại giúp mình nhé.") from exc
    except storage.ObjectTooLarge as exc:
        raise AttachmentRejected(422, "invalid_attachment", "Ảnh quá lớn, bạn chụp lại hoặc gửi ảnh nhỏ hơn nhé.") from exc
    except storage.StorageError as exc:
        raise AttachmentRejected(503, "unavailable", "Hệ thống đang bận, vui lòng thử lại sau ít phút.") from exc
    try:
        # Pillow chạy CPU: đẩy sang thread để không chặn event loop (SSE, request khác).
        await asyncio.to_thread(image.validate, data)
    except image.InvalidImage as exc:
        raise AttachmentRejected(422, "invalid_attachment", "Tệp gửi lên không phải ảnh JPEG, PNG hoặc WebP.") from exc

    digest = hashlib.sha256(data).hexdigest()
    async with db_session() as db:
        doc = await repo.find_by_hash(db, user_id, digest)
        if doc is None:
            doc = await repo.create_document(
                db, document_id=uuid4(), user_id=user_id, conversation_id=conversation_id,
                profile_id=profile_id, storage_path=path, sha256=digest,
            )
        else:
            await repo.reopen(db, doc, conversation_id)
        document_id, status, kept_path = doc.id, doc.status, doc.storage_path

    if status == "received":
        await pipeline.stash_image(redis, document_id, data)
    if kept_path != path:
        # Ảnh trùng: giữ bản cũ, xoá object vừa upload (đang có token user).
        try:
            await storage.delete(path, access_token)
        except storage.StorageError:
            logger.warning("Chưa xoá được ảnh upload trùng của tài liệu %s", document_id)
    return Accepted(document_id, status)
