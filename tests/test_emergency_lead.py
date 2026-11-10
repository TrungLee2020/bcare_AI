import pytest

from app.schemas import ChatRequestMessage
from app.services import answering, openai_client, output_validator
from tests.fake_openai import FakeOpenAI, make_answer

LEAD = "Hãy gọi 115 hoặc đến cơ sở y tế gần nhất ngay."


@pytest.mark.parametrize(
    "question, urgent",
    [
        ("Con tôi 2 tháng tuổi sốt 38.5 độ", True),
        ("bé nhà tôi 3 tuần tuổi bị sốt", True),
        ("Bé sơ sinh sốt nhẹ", True),
        ("Con tôi 2 tuổi bị sốt", False),
        ("Bố tôi tự nhiên méo miệng, nói ngọng", True),
        ("Đau thắt ngực lan ra tay trái", True),
        ("Mẹ tôi khó thở, môi tím tái", True),
        ("Con tôi đang co giật", True),
        ("Tôi muốn tự tử", True),
        ("Tôi hơi khó thở khi leo cầu thang", False),
        ("Tôi bị đau đầu 3 ngày", False),
    ],
)
def test_urgent_question(question, urgent):
    assert output_validator.urgent_question(question) is urgent


async def ask(question, answer_text, should_see_doctor=False):
    openai_client.set_openai(FakeOpenAI(answer=make_answer(answer=answer_text, should_see_doctor=should_see_doctor)))
    try:
        return await answering.answer_question(ChatRequestMessage(user_id="u1", tier="free", content=question))
    finally:
        openai_client.set_openai(None)


async def test_lead_sentence_added_when_first_sentence_has_no_action():
    resp = await ask("Con tôi 2 tháng tuổi sốt 38.5 độ", "Mình khuyến cáo bạn đưa bé đi khám ngay. Giữ bé thoáng mát.")
    assert resp.status == "ok"
    assert resp.answer.answer == f"{LEAD} Mình khuyến cáo bạn đưa bé đi khám ngay. Giữ bé thoáng mát."
    assert resp.answer.should_see_doctor is True


async def test_no_lead_when_first_sentence_already_says_115():
    text = "Bạn hãy gọi 115 ngay. Trong lúc chờ, để bố nằm nghiêng."
    resp = await ask("Bố tôi tự nhiên méo miệng, nói ngọng", text, should_see_doctor=True)
    assert resp.answer.answer == text


async def test_no_lead_for_ordinary_question():
    text = "Đau đầu vài ngày thường do căng thẳng hoặc thiếu ngủ. Bạn nên nghỉ ngơi."
    resp = await ask("Tôi bị đau đầu 3 ngày", text)
    assert resp.answer.answer == text
