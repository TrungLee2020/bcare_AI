"""Câu "chỉ tham khảo" (KAN-134). App hiển thị `settings.chat_disclaimer` cố
định dưới mỗi tin; khi đã bật `chat_disclaimer_in_app` thì xoá câu Bụt tự viết
để không lặp. Chỉ xoá câu đứng riêng, không đụng câu khác có chữ "tham khảo"."""

import re

from app.config import settings
from app.services.input_filter import normalize

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_DISCLAIMER = re.compile(r"tham khao.{0,60}khong thay the")


def strip(text: str) -> str:
    sentences = _SENTENCE_SPLIT.split(text.strip())
    kept = [s for s in sentences if not _DISCLAIMER.search(normalize(s))]
    return " ".join(kept).strip() if len(kept) != len(sentences) else text.strip()


def for_display(text: str) -> str:
    return strip(text) if settings.chat_disclaimer_in_app else text
