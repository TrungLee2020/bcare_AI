"""
Lớp lọc input độc lập, chạy TRƯỚC khi gửi câu hỏi sang OpenAI.

Đây là defense-in-depth, KHÔNG phải hàng rào chính. Regex luôn có thể bị lách
(viết lái, chèn ký tự lạ, dịch sang ngôn ngữ khác, mã hoá base64...). Hàng rào
chính vẫn là system prompt (coi input là dữ liệu, không phải chỉ dẫn) và lớp
validate output. Giá trị thật của lớp này là chặn sớm các mẫu injection phổ
biến để khỏi tốn tiền gọi API, và tạo tín hiệu để theo dõi/cảnh báo.
"""

import re
import unicodedata
from dataclasses import dataclass, field


def normalize(text: str) -> str:
    """
    Bỏ dấu tiếng Việt + gom khoảng trắng, để 1 pattern bắt được cả "bỏ qua mọi
    hướng dẫn" lẫn "bo qua moi huong dan" (người cố tình injection hay viết
    không dấu để né filter).
    """
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    # ký tự zero-width thường được chèn vào giữa từ để cắt pattern
    text = re.sub(r"[​-‏﻿]", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


# (reason, pattern) — pattern viết ở dạng đã bỏ dấu, khớp với normalize().
_INJECTION_PATTERNS: list[tuple[str, str]] = [
    # Ghi đè chỉ dẫn
    ("instruction_override", r"\b(bo qua|phot lo|khong can quan tam)\b.{0,30}\b(huong dan|chi dan|quy tac|quy dinh|yeu cau tren)\b"),
    ("instruction_override", r"\b(quen|xoa)\b.{0,20}\b(het|moi|tat ca)\b.{0,20}\b(huong dan|chi dan|nhung gi|o tren)\b"),
    ("instruction_override", r"\bignore\b.{0,30}\b(previous|prior|above|all|earlier)\b.{0,20}\b(instruction|prompt|rule|direction)"),
    ("instruction_override", r"\b(disregard|override|forget)\b.{0,30}\b(instruction|prompt|rule|guideline|everything)"),
    ("instruction_override", r"\bkhong can tuan theo\b|\bno longer bound by\b"),
    # Moi truy xuất system prompt
    ("prompt_extraction", r"\bsystem prompt\b|\bprompt he thong\b|\bprompt cua ban\b"),
    ("prompt_extraction", r"\b(in ra|hien thi|cho (toi|tui|minh) xem|doc lai|nhac lai|tiet lo)\b.{0,30}\b(huong dan|chi dan|prompt|quy tac)\b"),
    ("prompt_extraction", r"\b(reveal|show|print|repeat|output|display)\b.{0,30}\b(your |the )?(instruction|prompt|system message|rule)"),
    ("prompt_extraction", r"\bban duoc dan (gi|nhu the nao)\b|\bcau (dau tien|thu nhat) trong prompt\b"),
    ("prompt_extraction", r"\bwhat (were|are) your (\w+ )?(instruction|rule|guideline)"),
    # Đổi vai / jailbreak
    ("role_override", r"\b(bay gio|tu gio|ke tu bay gio)\b.{0,20}\bban la\b"),
    ("role_override", r"\bban (khong con|khong phai) la\b.{0,30}\b(tro ly|ai|chatbot)\b"),
    ("role_override", r"\byou are (now|no longer)\b"),
    ("role_override", r"\b(act as|pretend to be|roleplay as|simulate being)\b"),
    ("role_override", r"\bdong vai\b.{0,30}\b(bac si|luat su|mot ai khac|nguoi khac|he thong)\b"),
    ("role_override", r"\b(dan mode|developer mode|jailbreak|do anything now|god mode)\b"),
    # Giả mạo khung hội thoại
    ("fake_system_turn", r"<\|.*?\|>|\[/?(system|inst)\]|<<sys>>"),
    ("fake_system_turn", r"^\s*(system|assistant)\s*:", ),
    ("fake_system_turn", r"###\s*(system|instruction|new instruction)"),
    # Gỡ rào an toàn
    ("safety_override", r"\b(khong co|bo|go)\b.{0,15}\b(gioi han|rang buoc|kiem duyet|bo loc)\b"),
    ("safety_override", r"\b(no|without)\b.{0,15}\b(restriction|filter|limitation|censorship|guardrail)"),
    ("safety_override", r"\bkhong duoc tu choi\b|\byou must not refuse\b|\bmust answer no matter what\b"),
]

_COMPILED = [(reason, re.compile(p, re.IGNORECASE | re.MULTILINE)) for reason, p in _INJECTION_PATTERNS]


@dataclass
class InputCheck:
    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    matches: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return self.reasons[0] if self.reasons else ""


def check(content: str) -> InputCheck:
    """Quét câu hỏi, trả về kết quả có bị coi là prompt injection hay không."""
    normalized = normalize(content)
    result = InputCheck()
    for reason, pattern in _COMPILED:
        found = pattern.search(normalized)
        if found:
            result.blocked = True
            if reason not in result.reasons:
                result.reasons.append(reason)
            result.matches.append(found.group(0)[:80])
    return result
