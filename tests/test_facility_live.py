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
    # Câu giữ lại (held-out): không xuất hiện làm ví dụ trong prompt facility_v3.
    ("móng tay vàng và dày lên", {"da_lieu"}),
    ("chuột rút bắp chân ban đêm", {"co_xuong_khop", "than_kinh"}),
    ("tê đầu lưỡi mấy hôm nay", {"than_kinh", "tai_mui_hong", "rang_ham_mat"}),
]

# Câu mơ hồ: không được sinh giả định.
VAGUE = ["người cứ thấy sao sao", "dạo này không khoẻ lắm"]


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
    assert not guess_codes & catalog.GUESS_EXCLUDED, (text, result.guesses)
    assert not {k for k, p in mapping.specialties.items() if p.assumed} & catalog.GUESS_EXCLUDED


@pytest.mark.parametrize("text", VAGUE)
async def test_cau_mo_ho_khong_sinh_gia_dinh(text):
    raw, _ = await openai_client.interpret_symptoms(
        text, suggest._system_prompt(), SymptomInterpretation
    )
    result = verify(raw, text)
    print("GUESSES:", text, "=>", result.guesses)
    assert result.guesses == [], (text, result.guesses)
