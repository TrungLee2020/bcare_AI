"""
Nạp system prompt từ file, có version.

Prompt để ở file riêng (`prompts/system_<version>.md`) chứ không hardcode trong
code vì nội dung prompt sẽ phải chỉnh liên tục khi thấy model trả lời chưa đạt —
tách ra thì sửa prompt không cần review/deploy lại code. Đổi version bằng biến
môi trường PROMPT_VERSION, và giữ lại file version cũ để rollback được.
"""

import re
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Số từ liên tiếp trùng với system prompt thì coi là bị lộ prompt. 8 đủ dài để
# không dính nhầm các cụm thông thường ("bạn nên đi khám càng sớm càng tốt"),
# đủ ngắn để bắt được khi model chép nguyên một câu trong hướng dẫn ra ngoài.
LEAK_SHINGLE_SIZE = 8


@lru_cache(maxsize=8)
def load_system_prompt(version: str) -> str:
    path = PROMPT_DIR / f"system_{version}.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"Không tìm thấy prompt version {version!r} tại {path}. "
            f"Các version có sẵn: {', '.join(available_versions()) or 'không có'}"
        )
    return path.read_text(encoding="utf-8").strip()


def available_versions() -> list[str]:
    return sorted(
        p.stem.removeprefix("system_") for p in PROMPT_DIR.glob("system_*.md")
    )


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower(), flags=re.UNICODE)


@lru_cache(maxsize=8)
def prompt_shingles(version: str, size: int = LEAK_SHINGLE_SIZE) -> frozenset[str]:
    """Tập các cụm `size` từ liên tiếp trong system prompt, dùng để phát hiện
    câu trả lời có chép lại nội dung hướng dẫn hệ thống hay không."""
    words = _words(load_system_prompt(version))
    return frozenset(
        " ".join(words[i : i + size]) for i in range(len(words) - size + 1)
    )


def leaks_system_prompt(answer: str, version: str) -> bool:
    words = _words(answer)
    shingles = prompt_shingles(version)
    return any(
        " ".join(words[i : i + LEAK_SHINGLE_SIZE]) in shingles
        for i in range(len(words) - LEAK_SHINGLE_SIZE + 1)
    )
