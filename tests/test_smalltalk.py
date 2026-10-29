import pytest

from app.services.smalltalk import GREETING_ANSWER, is_greeting


@pytest.mark.parametrize(
    "text",
    [
        "hi", "Hi!", "hello", "Helooo", "hiii", "hey", "Alo", "a lô",
        "chào", "Chào bạn", "chào bạn!!", "Xin chào", "xin chào Bụt ạ", "chào Bụt nhé",
        "chao ban", "xin chao", "Chào buổi sáng", "Good morning", "hi there", "Hello bcare",
        "chào em", "Bụt ơi chào bạn", "  hello  ",
    ],
)
def test_nhan_ra_cau_chao(text):
    assert is_greeting(text)


@pytest.mark.parametrize(
    "text",
    [
        "Chào bạn, tôi bị đau đầu 3 ngày",
        "hi, huyết áp 150/95 có sao không",
        "Tôi bị đau đầu",
        "chào bạn tôi muốn hỏi về bảo hiểm",
        "bạn ơi",          # gọi nhưng không chào, để model hỏi lại
        "cảm ơn",
        "",
        "👋",
        "hihi",            # cười, không phải chào
        "chi phí khám",    # "chi" là từ đệm nhưng không có lời chào
    ],
)
def test_khong_bat_nham(text):
    assert not is_greeting(text)


def test_cau_chao_lai_gioi_thieu_but():
    assert "Mình là Bụt" in GREETING_ANSWER.answer
    assert len(GREETING_ANSWER.follow_up_questions) == 3
