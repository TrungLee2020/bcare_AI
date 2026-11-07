"""Câu "chỉ tham khảo" (KAN-134). App hiển thị `settings.chat_disclaimer` cố
định dưới mỗi tin; khi đã bật `chat_disclaimer_in_app` thì xoá câu Bụt tự viết
để không lặp. Chỉ xoá câu đứng riêng, không đụng câu khác có chữ "tham khảo"."""

import re

from app.config import settings
from app.services.input_filter import normalize

# Ranh giới câu: sau dấu kết câu + khoảng trắng, hoặc xuống dòng.
_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")
# Hình dạng câu miễn trừ (đã bỏ dấu): dạng đầy đủ "thông tin … tham khảo …
# không thay thế … khám/chữa bệnh/bác sĩ/y tế" và dạng ngắn "(đây) (chỉ) là
# (thông tin) tham khảo, không thay thế (việc) khám…". Không bắt lời khuyên
# kiểu "tham khảo bác sĩ… thực phẩm chức năng không thay thế thuốc".
_TAIL = r"(kham|chua benh|bac si|y te)\b"
_DISCLAIMER = re.compile(
    r"\bthong tin\b.{0,40}\btham khao\b.{0,30}\bkhong thay the\b.{0,20}\b" + _TAIL
    + r"|\b(day )?(chi )?la (thong tin )?tham khao,? khong thay the (viec )?" + _TAIL
)


def strip(text: str) -> str:
    """Xoá đúng (các) câu miễn trừ cùng khoảng trắng ngay trước nó; mọi ký tự
    khác (xuống dòng, gạch đầu dòng) giữ nguyên."""
    spans = []  # (start, end) của từng câu trong text gốc
    pos = 0
    for m in _BOUNDARY.finditer(text):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(text)))
    out = text
    for start, end in reversed(spans):
        if start == end or not _DISCLAIMER.search(normalize(text[start:end])):
            continue
        cut = start
        while cut > 0 and text[cut - 1].isspace():
            cut -= 1
        # Câu đầu văn bản: bỏ luôn khoảng trắng phía sau để không để lại dòng trống.
        tail_start = end
        if cut == 0:
            while tail_start < len(out) and out[tail_start].isspace():
                tail_start += 1
        out = out[:cut] + out[tail_start:]
    return out.rstrip() if out != text else text.strip()


def for_display(text: str) -> str:
    return strip(text) if settings.chat_disclaimer_in_app else text
