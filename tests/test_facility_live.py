"""
Test chạy với OpenAI THẬT: đo việc "Bụt giả định" chuyên khoa cho cụm ngoài bảng
(KAN-134). Mặc định bị skip. Chạy:

    OPENAI_API_KEY=sk-... pytest -m live tests/test_facility_live.py -q
"""

import os

import pytest

from app.services import openai_client
from app.services.facility import catalog, suggest
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
    mapping = catalog.map_symptoms(
        [k for k, _ in result.symptoms],
        "adult",
        after_trauma=result.after_trauma,
        mental_health=result.mental_health,
        guesses=result.guesses,
    )
    print("GUESSES:", text, "=>", result.guesses, "MAPPED:", list(mapping.specialties))
    # Bảng hay giả định đều tính: đến đúng chuyên khoa là đạt; bảng thắng giả định là chủ đích.
    assert set(mapping.specialties) & accepted, (text, result, mapping.specialties)
    guess_codes = {code for _, code, _ in result.guesses}
    assert guess_codes <= accepted, (text, result.guesses)
    assert not guess_codes & {"tam_than", "nhi"}, (text, result.guesses)
    assert not {k for k, p in mapping.specialties.items() if p.assumed} & {"tam_than", "nhi"}
