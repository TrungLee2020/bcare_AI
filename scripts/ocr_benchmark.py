"""
Đo token và độ chính xác của bước OCR theo kích thước ảnh và model, để chốt
OCR_MAX_SIDE và OCR_EXTRACT_MODEL (spec mục 4, 10).

    python scripts/ocr_benchmark.py <thư mục ảnh có manifest.json> --sides 1024 1280 1600 2048 --models gpt-5-nano gpt-6-luna

Cần OPENAI_API_KEY trong môi trường. In bảng: model, cạnh dài, token đầu vào
trung bình, số trường đúng / tổng.
"""

import argparse
import asyncio
import json
from pathlib import Path

from app.config import settings
from app.services import openai_client
from app.services.documents import image


async def run(folder: Path, sides: list[int], models: list[str]) -> None:
    manifest = [m for m in json.loads((folder / "manifest.json").read_text(encoding="utf-8")) if m["expected"]]
    openai_client.start_openai()
    print("model | max_side | prompt_tokens TB | đúng/tổng")
    for model in models:
        settings.ocr_extract_model = model
        for side in sides:
            tokens, hit, total = [], 0, 0
            for sample in manifest:
                prepared = image.prepare((folder / sample["file"]).read_bytes(), side, settings.ocr_max_crop_ratio)
                result, usage = await openai_client.extract_document(prepared.data_url, sample["doc_type"])
                tokens.append(usage.prompt_tokens)
                text = (result.ocr_text + " " + str(result.model_dump())).lower()
                for value in sample["expected"].values():
                    for item in value if isinstance(value, list) else [value]:
                        total += 1
                        hit += str(item).lower() in text
            print(f"{model} | {side} | {sum(tokens) // len(tokens)} | {hit}/{total}")
    await openai_client.stop_openai()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("--sides", type=int, nargs="+", default=[1024, 1280, 1600, 2048])
    parser.add_argument("--models", nargs="+", default=[settings.openai_model])
    args = parser.parse_args()
    asyncio.run(run(args.folder, args.sides, args.models))
