import pytest

from app.config import settings
from app.services import openai_client
from app.services.documents.schemas import EXTRACTION_SCHEMAS, Classification
from tests.fake_vision import FakeVision

DATA_URL = "data:image/jpeg;base64,AAAA"


@pytest.fixture
def vision():
    fake = FakeVision()
    openai_client.set_openai(fake)
    yield fake
    openai_client.set_openai(None)


def _walk(schema: dict, defs: dict):
    if "$ref" in schema:
        yield from _walk(defs[schema["$ref"].split("/")[-1]], defs)
        return
    if schema.get("type") == "object":
        yield schema
        for sub in schema.get("properties", {}).values():
            yield from _walk(sub, defs)
    for key in ("anyOf", "allOf"):
        for sub in schema.get(key, []):
            yield from _walk(sub, defs)
    if "items" in schema:
        yield from _walk(schema["items"], defs)


@pytest.mark.parametrize("model", [Classification, *EXTRACTION_SCHEMAS.values()])
def test_schemas_are_valid_for_strict_mode(model):
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    for obj in _walk(schema, defs):
        assert obj.get("additionalProperties") is False
        assert set(obj["required"]) == set(obj["properties"])


async def test_classify_sends_low_detail_image_to_classify_model(vision, monkeypatch):
    monkeypatch.setattr(settings, "ocr_classify_model", "classify-model")
    result, usage = await openai_client.classify_document(DATA_URL)
    assert result.doc_type == "prescription"
    assert usage.prompt_tokens == 300
    call = vision.calls[0]
    assert call["model"] == "classify-model"
    image_part = call["messages"][1]["content"][1]
    assert image_part == {"type": "image_url", "image_url": {"url": DATA_URL, "detail": "low"}}
    assert vision.names() == ["document_classification"]


async def test_extract_sends_high_detail_image_with_type_schema(vision, monkeypatch):
    monkeypatch.setattr(settings, "ocr_extract_model", "")
    monkeypatch.setattr(settings, "openai_model", "chat-model")
    result, _ = await openai_client.extract_document(DATA_URL, "prescription")
    assert result.medications[0].name == "Paracetamol"
    call = vision.calls[0]
    assert call["model"] == "chat-model"
    assert call["messages"][1]["content"][1]["image_url"]["detail"] == "high"
    assert "prescription" in call["messages"][0]["content"]
    assert call["response_format"]["json_schema"]["schema"]["title"] == "PrescriptionExtraction"
    assert call["max_completion_tokens"] == settings.ocr_max_output_tokens + settings.openai_reasoning_token_budget


async def test_extract_model_override(monkeypatch):
    monkeypatch.setattr(settings, "ocr_extract_model", "vision-big")
    assert openai_client.extract_model() == "vision-big"
