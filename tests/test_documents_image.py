import base64
import io

import pytest
from PIL import Image

from app.services.documents import image


def encode(img: Image.Image, fmt: str = "JPEG", **kwargs) -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt, **kwargs)
    return buf.getvalue()


def paper_on_table(size=(1000, 800), paper=(200, 100, 800, 700)) -> Image.Image:
    """Tờ giấy trắng trên mặt bàn xám, có một vạch đen làm "chữ"."""
    img = Image.new("RGB", size, (90, 90, 90))
    img.paste((255, 255, 255), paper)
    left, top, right, _ = paper
    img.paste((0, 0, 0), (left + 50, top + 50, right - 50, top + 80))
    return img


def test_load_rejects_bytes_that_are_not_an_image():
    with pytest.raises(image.InvalidImage):
        image.load(b"not an image")


def test_load_rejects_unsupported_format():
    with pytest.raises(image.InvalidImage):
        image.load(encode(Image.new("RGB", (10, 10)), "GIF"))


def test_load_applies_exif_rotation():
    exif = Image.Exif()
    exif[0x0112] = 6  # xoay 90 độ
    data = encode(Image.new("RGB", (200, 100), "white"), "JPEG", exif=exif)
    assert image.load(data).size == (100, 200)


def test_crop_removes_table_around_paper():
    assert image.crop_borders(paper_on_table(), 0.2).size == (600, 600)


def test_crop_never_cuts_more_than_max_ratio_per_side():
    small_paper = paper_on_table(paper=(400, 300, 600, 500))
    assert image.crop_borders(small_paper, 0.2).size == (600, 480)


def test_crop_keeps_uniform_image():
    assert image.crop_borders(Image.new("RGB", (300, 200), "white"), 0.2).size == (300, 200)


def test_resize_shrinks_long_side_keeping_ratio():
    assert image.resize(Image.new("RGB", (3000, 1500)), 1600).size == (1600, 800)


def test_resize_never_upscales():
    assert image.resize(Image.new("RGB", (800, 600)), 1600).size == (800, 600)


def test_prepare_returns_small_jpeg_data_url():
    # PNG: nén JPEG làm nhiễu mép giấy, lệch kích thước sau cắt viền.
    data = encode(paper_on_table(size=(3000, 2400), paper=(600, 300, 2400, 2100)), "PNG")
    prepared = image.prepare(data, 512, 0.2)
    assert prepared.data_url.startswith("data:image/jpeg;base64,")
    decoded = Image.open(io.BytesIO(base64.b64decode(prepared.data_url.split(",", 1)[1])))
    assert decoded.size == (prepared.width, prepared.height) == (512, 512)


def test_prepare_strips_exif():
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    data = encode(Image.new("RGB", (400, 300), "white"), "JPEG", exif=exif)
    prepared = image.prepare(data, 512, 0.2)
    decoded = Image.open(io.BytesIO(base64.b64decode(prepared.data_url.split(",", 1)[1])))
    assert len(decoded.getexif()) == 0
