import pytest

from app import metrics
from app.services import answering, openai_client, output_validator
from app.schemas import ChatRequestMessage
from tests.fake_openai import FakeOpenAI, make_answer


@pytest.mark.parametrize(
    "text, names",
    [
        ("Uống vitamin C buổi tối được, không sao cả.", ["khong_sao_ca"]),
        ("Uong vitamin C buoi toi duoc, khong sao ca.", ["khong_sao_ca"]),
        ("Thuốc này chắc chắn an toàn và hoàn toàn bình thường.", ["chac_chan", "hoan_toan_binh_thuong"]),
        ("Hoàn toàn an toàn cho mọi người.", ["hoan_toan_an_toan"]),
        ("Tuyệt đối an toàn.", ["tuyet_doi"]),
        ("Hiệu quả 100% sau 3 ngày.", ["100_phan_tram"]),
        ("Đảm bảo khỏi bệnh.", ["dam_bao_khoi"]),
        ("Với đa số người, uống sau bữa tối thường không gây khó chịu.", []),
        ("Tuyệt đối không tự ý tăng liều.", []),
    ],
)
def test_absolute_claims(text, names):
    assert output_validator.absolute_claims(text) == names


async def test_answering_counts_but_does_not_change_absolute_answer():
    metrics.reset()
    text = "Uống vitamin C buổi tối được, không sao cả. Đây là thông tin tham khảo, không thay thế khám chữa bệnh trực tiếp."
    openai_client.set_openai(FakeOpenAI(answer=make_answer(answer=text)))
    try:
        resp = await answering.answer_question(ChatRequestMessage(user_id="u1", tier="free", content="vitamin C buổi tối được không"))
    finally:
        openai_client.set_openai(None)
    assert resp.status == "ok"
    assert resp.answer.answer == text
    assert metrics.snapshot()["answer_absolute_claim"] == 1
    assert metrics.snapshot()["answer_absolute_claim_khong_sao_ca"] == 1
