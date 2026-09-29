"""
Dựng prompt: nạp system prompt từ file (có version) và bọc dữ liệu không tin cậy.

Prompt để ở file riêng (`prompts/system_<version>.md`) chứ không hardcode trong
code vì nội dung prompt sẽ phải chỉnh liên tục khi thấy model trả lời chưa đạt —
tách ra thì sửa prompt không cần review/deploy lại code. Đổi version bằng biến
môi trường PROMPT_VERSION, và giữ lại file version cũ để rollback được.

Module này cũng giữ phần `fence()` — cách DUY NHẤT được phép để nhét nội dung do
người dùng (hoặc do model sinh ra) vào prompt. Xem docstring của `fence()`.
"""

import re
from functools import lru_cache
from pathlib import Path

from app.services.input_filter import normalize

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

# Số từ liên tiếp trùng với system prompt thì coi là bị lộ prompt. 8 đủ dài để
# không dính nhầm các cụm thông thường ("bạn nên đi khám càng sớm càng tốt"),
# đủ ngắn để bắt được khi model chép nguyên một câu trong hướng dẫn ra ngoài.
LEAK_SHINGLE_SIZE = 8

# Câu mà system prompt YÊU CẦU model nói với người dùng. Model làm đúng thì sẽ
# chép gần nguyên văn, nên các cụm nằm gọn trong những câu này không được tính
# là lộ prompt (nếu không, câu trả lời đúng hướng dẫn lại bị chặn). Sửa câu tương
# ứng trong prompts/ thì phải sửa ở đây.
ALLOWED_PHRASES = (
    "đây là thông tin tham khảo, không thay thế khám chữa bệnh trực tiếp",
    # system_v4: dấu hiệu cấp cứu / ý định tự hại. Thiếu 2 câu này thì câu trả
    # lời khuyên đi cấp cứu đúng hướng dẫn bị chặn là "lộ prompt" — đo với
    # gpt-5.6-luna: 3/12 câu trả lời bình thường bị chặn nhầm.
    "gọi 115 hoặc đến cơ sở y tế gần nhất ngay",
    "gọi 115 hoặc tới cơ sở y tế gần nhất",
    # system_v4: câu hỏi quyền lợi của gói cụ thể
    "xem Bảng quyền lợi / Quy tắc bảo hiểm của mình hoặc gọi tổng đài bCare",
    # system_v4: thuốc không kê đơn
    "theo hướng dẫn trên bao bì, dược sĩ hoặc bác sĩ",
)

# Các thẻ có ý nghĩa cấu trúc trong prompt. Nội dung không tin cậy TUYỆT ĐỐI
# không được chứa chúng — xem `fence()`.
RESERVED_TAGS = (
    "user_question",
    "session_summary",
    "transcript",
    "system",
    "assistant",
    "user",
    "im_start",
    "im_end",
)

_RESERVED_TAG_RE = re.compile(
    r"<\s*/?\s*(?:\|\s*)?(?:" + "|".join(RESERVED_TAGS) + r")\s*(?:\|\s*)?/?\s*>",
    re.IGNORECASE,
)

# Chỗ thay cho thẻ bị gỡ: cố ý để lại dấu vết nhìn thấy được thay vì xoá trắng,
# để model hiểu "chỗ này có thứ đã bị lược" chứ không đọc liền mạch phần trước
# với phần sau thành một câu mới.
REDACTED = "[đã lược bỏ thẻ]"

TRUNCATED = "…[đã cắt bớt]"


def sanitize_untrusted(text: str) -> str:
    """
    Gỡ các thẻ cấu trúc ra khỏi nội dung không tin cậy.

    Không gỡ thì người dùng chỉ cần gõ `</user_question>` là thoát ra khỏi khối
    dữ liệu, và mọi thứ họ viết sau đó nằm NGOÀI khối — đúng vị trí mà system
    prompt coi là chỉ dẫn. Đây là lỗ hổng nghiêm trọng hơn mọi mẫu regex trong
    input_filter, vì nó không cần model "bị dẫn dụ": khung prompt đã sai từ lúc
    dựng chuỗi.

    Chỉ đụng tới đúng các thẻ trong RESERVED_TAGS, nên "huyết áp <120" hay
    "chỉ số < 5" của người dùng thật vẫn giữ nguyên.
    """
    return _RESERVED_TAG_RE.sub(REDACTED, text)


def clip(text: str, max_chars: int | None) -> str:
    """Cắt bớt kèm dấu vết. Cắt lặng lẽ thì model đọc phần cụt như một câu hoàn
    chỉnh và có thể suy ra điều người dùng không hề nói."""
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + TRUNCATED


def fence(
    tag: str, content: str, max_chars: int | None = None, *, sanitize: bool = True
) -> str:
    """
    Bọc `content` (dữ liệu không tin cậy) vào khối `<tag>...</tag>`.

    `max_chars` chặn trên độ dài để một message dài bất thường trong lịch sử
    không tự nhân chi phí của mọi câu hỏi sau trong phiên.

    `sanitize=False` CHỈ dành cho `content` được ghép hoàn toàn từ các khối
    `fence()` khác (vd transcript gồm các khối `<user>`/`<assistant>`): các khối
    con đã được gỡ thẻ rồi, gỡ lần nữa là xoá luôn chính các thẻ cấu trúc mình
    vừa dựng.
    """
    if tag not in RESERVED_TAGS:
        raise ValueError(f"Thẻ {tag!r} không nằm trong RESERVED_TAGS")
    if sanitize:
        content = sanitize_untrusted(content)
    return f"<{tag}>\n{clip(content.strip(), max_chars)}\n</{tag}>"

@lru_cache(maxsize=16)
def load_prompt(kind: str, version: str) -> str:
    """`kind` là tiền tố file: "system" -> prompts/system_<version>.md,
    "summary" -> prompts/summary_<version>.md."""
    path = PROMPT_DIR / f"{kind}_{version}.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"Không tìm thấy prompt {kind!r} version {version!r} tại {path}. "
            f"Các version có sẵn: {', '.join(available_versions(kind)) or 'không có'}"
        )
    return path.read_text(encoding="utf-8").strip()


def load_system_prompt(version: str) -> str:
    return load_prompt("system", version)


def available_versions(kind: str = "system") -> list[str]:
    return sorted(
        p.stem.removeprefix(f"{kind}_") for p in PROMPT_DIR.glob(f"{kind}_*.md")
    )


def _words(text: str) -> list[str]:
    # Bỏ dấu trước khi cắt từ: model chép lại hướng dẫn nhưng viết không dấu
    # (hoặc bị mất dấu khi đi qua một bước xử lý nào đó) vẫn phải bị bắt.
    return re.findall(r"\w+", normalize(text), flags=re.UNICODE)


def _shingles(words: list[str], size: int) -> set[str]:
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


@lru_cache(maxsize=16)
def prompt_shingles(
    kind: str, version: str, size: int = LEAK_SHINGLE_SIZE
) -> frozenset[str]:
    """Tập các cụm `size` từ liên tiếp trong prompt, dùng để phát hiện văn bản
    có chép lại nội dung hướng dẫn hay không."""
    shingles = _shingles(_words(load_prompt(kind, version)), size)
    if kind == "system":
        allowed = set().union(*(
            _shingles(_words(phrase), size) for phrase in ALLOWED_PHRASES
        ))
        shingles -= allowed
    return frozenset(shingles)


@lru_cache(maxsize=1)
def _allowed_word_seqs() -> tuple[tuple[str, ...], ...]:
    # Dài trước: câu dài chứa câu ngắn thì cắt câu dài.
    seqs = {tuple(_words(phrase)) for phrase in ALLOWED_PHRASES}
    return tuple(sorted(seqs, key=len, reverse=True))


def _without_allowed(words: list[str]) -> list[list[str]]:
    """Cắt các câu trong ALLOWED_PHRASES ra khỏi văn bản, trả về các đoạn còn
    lại. Chỉ trừ shingle nằm GỌN trong câu được phép (ở `prompt_shingles`) là
    chưa đủ: model viết tiếp tự nhiên sau câu đó ("...dược sĩ hoặc bác sĩ,
    không tự ý...") tạo ra shingle vắt qua ranh giới, trùng với chữ đầu dòng kế
    tiếp của prompt ("Không khuyên ngừng...") -> câu trả lời đúng vẫn bị chặn."""
    segments, current, i = [], [], 0
    while i < len(words):
        for seq in _allowed_word_seqs():
            if tuple(words[i : i + len(seq)]) == seq:
                segments.append(current)
                current = []
                i += len(seq)
                break
        else:
            current.append(words[i])
            i += 1
    segments.append(current)
    return segments


def leaks_prompt(
    text: str, kind: str, version: str, size: int = LEAK_SHINGLE_SIZE
) -> bool:
    """Văn bản có chứa `size` từ liên tiếp trùng với prompt `kind`/`version`?"""
    words = _words(text)
    segments = _without_allowed(words) if kind == "system" else [words]
    shingles = set().union(*(_shingles(seg, size) for seg in segments))
    return bool(shingles & prompt_shingles(kind, version, size))


def leaks_system_prompt(text: str, version: str) -> bool:
    return leaks_prompt(text, "system", version)
