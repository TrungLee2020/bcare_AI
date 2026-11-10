"""Ngữ cảnh `document_data` cho một hội thoại: dữ liệu đã trích từ ảnh user
gửi trong hội thoại đó, hoặc lý do chưa đọc được (spec mục 3.4). Chỉ bản
trích xuất, không chữ OCR thô: ngắn hơn, ít chỗ cho câu injection trong ảnh."""

import json
from uuid import UUID

from app.config import settings
from app.db import documents_repository as repo
from app.db.models import HealthDocument
from app.db.session import db_session
from app.services.documents.pipeline import TYPE_LABELS

STATUS_NOTES = {
    "received": "đang đọc, chưa xong",
    "processing": "đang đọc, chưa xong",
    "skipped": "không phải giấy tờ y tế nên Bụt không đọc",
    "unreadable": "ảnh mờ hoặc bị che, không đọc được; cần chụp lại rõ hơn",
    "quota_exceeded": "chưa đọc vì hôm nay đã hết lượt đọc ảnh; gửi lại ảnh này vào ngày mai",
    "failed": "đọc bị lỗi; cần gửi lại ảnh",
}


def render(rows: list[tuple[HealthDocument, dict | None]]) -> str:
    blocks = []
    for doc, data in rows:
        name = doc.title or TYPE_LABELS.get(doc.doc_type or "", "Ảnh đã gửi")
        if doc.status == "done" and data is not None:
            body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        elif doc.status == "done":
            body = "Phim chụp: chỉ lưu ảnh, Bụt không đọc hay diễn giải phim."
        else:
            body = STATUS_NOTES.get(doc.status, "chưa đọc được")
        blocks.append(f"### {name}\n{body}")
    return "\n\n".join(blocks)


async def for_conversation(session_id: UUID | None, user_id: str) -> str | None:
    if session_id is None:
        return None
    async with db_session() as db:
        rows = await repo.conversation_documents(db, session_id, user_id, settings.ocr_context_documents)
    return render(rows) or None
