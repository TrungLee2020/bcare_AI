"""Đọc ảnh với OpenAI THẬT trên ảnh giả lập (scripts/ocr_samples.py). Mặc
định bị skip; chạy khi sửa prompt OCR hoặc đổi model:

    python -m pytest -m live tests/test_documents_live.py -q
"""

import os
import sys
from pathlib import Path

import pytest

from app.config import settings
from app.services import openai_client
from app.services.documents import image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ocr_samples  # noqa: E402

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="cần OPENAI_API_KEY để gọi API thật"),
]


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("ocr_samples")
    return out, ocr_samples.generate(out)


@pytest.fixture(autouse=True)
def live_client():
    openai_client.start_openai()
    yield
    openai_client.set_openai(None)


def _prepared(out, sample, side):
    return image.prepare((out / sample["file"]).read_bytes(), side, settings.ocr_max_crop_ratio)


async def test_classification(samples):
    out, manifest = samples
    wrong = []
    for sample in manifest:
        found, _ = await openai_client.classify_document(_prepared(out, sample, settings.ocr_classify_side).data_url)
        if found.doc_type != sample["doc_type"] or found.readable != sample["readable"]:
            wrong.append((sample["file"], found.doc_type, found.readable))
    assert wrong == []


async def test_extraction_reads_expected_fields(samples):
    out, manifest = samples
    misses = []
    for sample in manifest:
        expected = sample["expected"]
        if not expected:
            continue
        result, _ = await openai_client.extract_document(_prepared(out, sample, settings.ocr_max_side).data_url, sample["doc_type"])
        data = result.model_dump()
        text = (data["ocr_text"] + " " + str(data)).lower()
        for key, value in expected.items():
            for item in value if isinstance(value, list) else [value]:
                if str(item).lower() not in text:
                    misses.append((sample["file"], key, item))
        if "doc_date" in expected and data["doc_date"] != expected["doc_date"]:
            misses.append((sample["file"], "doc_date", data["doc_date"]))
    assert misses == []
