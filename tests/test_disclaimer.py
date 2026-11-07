import pytest

from app.config import settings
from app.prompts import APP_DISCLAIMER_MARKER, load_system_prompt
from app.services import disclaimer

RULE = "Luôn nhắc đây là thông tin tham khảo"


def test_v8_keeps_rule_and_hides_marker_when_app_does_not_show_disclaimer(monkeypatch):
    monkeypatch.setattr(settings, "chat_disclaimer_in_app", False)
    prompt = load_system_prompt("v8")
    assert RULE in prompt
    assert APP_DISCLAIMER_MARKER not in prompt


def test_v8_drops_rule_when_app_shows_disclaimer(monkeypatch):
    monkeypatch.setattr(settings, "chat_disclaimer_in_app", True)
    prompt = load_system_prompt("v8")
    assert RULE not in prompt
    assert APP_DISCLAIMER_MARKER not in prompt
    assert "## Nói theo điều kiện" in prompt


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "Bạn nên nghỉ ngơi. Đây là thông tin tham khảo, không thay thế khám chữa bệnh trực tiếp.",
            "Bạn nên nghỉ ngơi.",
        ),
        ("Bạn nên nghỉ ngơi.\n\nĐây chỉ là tham khảo, không thay thế khám chữa bệnh.", "Bạn nên nghỉ ngơi."),
        ("Thuốc tham khảo giá ở nhà thuốc. Bạn nên hỏi dược sĩ.", "Thuốc tham khảo giá ở nhà thuốc. Bạn nên hỏi dược sĩ."),
    ],
)
def test_strip(text, expected):
    assert disclaimer.strip(text) == expected


def test_for_display_only_strips_when_flag_on(monkeypatch):
    text = "Nghỉ ngơi nhé. Đây là thông tin tham khảo, không thay thế khám chữa bệnh trực tiếp."
    monkeypatch.setattr(settings, "chat_disclaimer_in_app", False)
    assert disclaimer.for_display(text) == text
    monkeypatch.setattr(settings, "chat_disclaimer_in_app", True)
    assert disclaimer.for_display(text) == "Nghỉ ngơi nhé."


def test_default_disclaimer_text():
    assert settings.chat_disclaimer == "Thông tin mang tính tham khảo, không thay thế khám chữa bệnh."


@pytest.mark.parametrize(
    "text",
    [
        "Bạn nên tham khảo ý kiến dược sĩ; thực phẩm chức năng không thay thế thuốc chữa bệnh.",
        "Hãy tham khảo bác sĩ vì thuốc bổ không thay thế được thuốc huyết áp.",
    ],
)
def test_strip_keeps_medical_advice_with_similar_words(text):
    assert disclaimer.strip(text) == text
    assert disclaimer.strip("Nghỉ ngơi nhé. " + text) == "Nghỉ ngơi nhé. " + text


@pytest.mark.parametrize(
    "sentence",
    [
        "Thông tin mang tính tham khảo, không thay thế khám chữa bệnh.",
        "Đây chỉ là tham khảo, không thay thế khám chữa bệnh.",
        "Đây là thông tin tham khảo, không thay thế khám chữa bệnh trực tiếp.",
    ],
)
def test_strip_removes_disclaimer_shapes(sentence):
    assert disclaimer.strip("Nghỉ ngơi nhé. " + sentence) == "Nghỉ ngơi nhé."


def test_strip_keeps_layout_of_other_text():
    text = "Gạch đầu dòng:\n- Uống nước.\n- Nghỉ ngơi.\n\nĐây là thông tin tham khảo, không thay thế khám chữa bệnh trực tiếp."
    assert disclaimer.strip(text) == "Gạch đầu dòng:\n- Uống nước.\n- Nghỉ ngơi."


def test_strip_middle_sentence_keeps_rest_as_is():
    text = "Ý một.\n\nThông tin mang tính tham khảo, không thay thế khám chữa bệnh. Ý hai.\n- mục"
    assert disclaimer.strip(text) == "Ý một. Ý hai.
- mục"
