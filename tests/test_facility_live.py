"""
Test chạy với OpenAI THẬT: đo việc "Bụt giả định" chuyên khoa cho cụm ngoài bảng
(KAN-134). Mặc định bị skip. Chạy:

    OPENAI_API_KEY=sk-... pytest -m live tests/test_facility_live.py -q
"""

import os

import pytest

from app.services import openai_client
from app.services.facility import suggest
from app.services.facility.interpret import SymptomInterpretation, verify

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.getenv("OPENAI_API_KEY"), reason="cần OPENAI_API_KEY để gọi API thật"
    ),
]

CASES = [
    ("đau gót chân mỗi sáng khi mới bước xuống giường", {"co_xuong_khop"}),
    ("mấy hôm nay nhìn một thành hai", {"mat", "than_kinh"}),
    ("ngứa hậu môn nhiều tuần nay", {"da_lieu", "ngoai_tong_quat", "tieu_hoa"}),
]


@pytest.fixture(autouse=True)
def live_client():
    openai_client.start_openai()
    yield
    openai_client.set_openai(None)


@pytest.mark.parametrize("text,accepted", CASES)
async def test_gia_dinh_chuyen_khoa_nam_trong_tap_chap_nhan(text, accepted):
    raw, _ = await openai_client.interpret_symptoms(
        text, suggest._system_prompt(), SymptomInterpretation
    )
    result = verify(raw, text)
    print("GUESSES:", text, "=>", result.guesses)
    assert result.guesses, (text, result)
    codes = {code for _, code, _ in result.guesses}
    assert codes <= accepted, (text, result.guesses)
    assert not codes & {"tam_than", "nhi"}, (text, result.guesses)
