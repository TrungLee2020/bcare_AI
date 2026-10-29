"""
Câu chào ("hi", "chào bạn", "xin chào Bụt ạ"...) trả lời cố định, không gọi
model và không tính lượt: user free chỉ có 2 câu/ngày, mất một câu cho "hello"
là phí, mà câu trả lời thì đã biết trước.

Chỉ bắt tin nhắn TOÀN là lời chào (cộng vài từ xưng hô/đệm). "Chào bạn, tôi bị
đau đầu" có nội dung hỏi thật nên vẫn đi qua model.
"""

import re

from app.schemas import ChatAnswer
from app.services.input_filter import normalize

# Viết không dấu, chữ thường. Chữ lặp ("helloooo", "hiii") được gom về 1 ký tự
# ở cả hai phía (`_squash`), nên chỉ cần liệt kê một dạng.
_GREETINGS = (
    "xin chao", "chao", "chao ban", "hello", "hi", "hey", "alo", "a lo",
    "chao buoi sang", "chao buoi trua", "chao buoi chieu", "chao buoi toi",
    "good morning", "good afternoon", "good evening", "hi there",
)
# Từ xưng hô / đệm được phép đi kèm lời chào mà vẫn tính là chỉ chào.
_FILLERS = {
    "ban", "but", "bcare", "ad", "admin", "bot", "em", "anh", "chi", "co", "chu",
    "bac", "ong", "ba", "moi", "nguoi", "tro", "ly", "a", "nhe", "nha", "oi", "ne",
    "there", "ca", "nha",
}
# Lời chào dài hơn ngần này từ thì nhiều khả năng có kèm câu hỏi.
_MAX_WORDS = 6


def _squash(word: str) -> str:
    return re.sub(r"(\w)\1+", r"\1", word)


_GREETING_SEQS = sorted(
    {tuple(_squash(w) for w in g.split()) for g in _GREETINGS}, key=len, reverse=True
)
_FILLER_WORDS = {_squash(w) for w in _FILLERS}


def is_greeting(text: str) -> bool:
    words = [_squash(w) for w in re.findall(r"\w+", normalize(text))]
    if not words or len(words) > _MAX_WORDS:
        return False
    greeted, i = False, 0
    while i < len(words):
        for seq in _GREETING_SEQS:
            if tuple(words[i : i + len(seq)]) == seq:
                greeted, i = True, i + len(seq)
                break
        else:
            if words[i] not in _FILLER_WORDS:
                return False
            i += 1
    return greeted


GREETING_ANSWER = ChatAnswer(
    answer=(
        "Chào bạn! Mình là Bụt, trợ lý sức khoẻ và bảo hiểm của bCare. Bạn có thể "
        "hỏi mình về các triệu chứng thường gặp, cách chăm sóc tại nhà, khi nào "
        "nên đi khám, hoặc quyền lợi và thủ tục bảo hiểm. Hôm nay bạn cần mình "
        "giúp gì?"
    ),
    out_of_scope=False,
    refusal_reason="",
    should_see_doctor=False,
    follow_up_questions=[
        "Đau đầu kéo dài mấy ngày thì nên đi khám?",
        "Huyết áp bao nhiêu thì được coi là cao?",
        "Thủ tục bồi thường bảo hiểm sức khoẻ gồm những bước nào?",
    ],
)
