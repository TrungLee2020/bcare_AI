"""
Chuẩn hoá ảnh giấy tờ trước khi gửi model (spec mục 4).

Token ảnh tính theo số điểm ảnh, không theo dung lượng file: giảm chất lượng
JPEG không bớt token, chỉ cắt viền và thu nhỏ mới bớt. Module thuần, không
I/O, để test được không cần mạng.
"""

import base64
import io
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})
JPEG_QUALITY = 85
# Chênh độ sáng so với màu nền (lấy ở 4 góc) để tính là "có nội dung".
BORDER_TOLERANCE = 24
# Trần số điểm ảnh, kiểm từ header trước khi giải mã: App gửi ảnh cạnh ≤ 2048 px
# (~4 MP), ảnh gốc điện thoại ~12–50 MP. Không có trần này, một PNG đồng màu
# ~150 MP vẫn lọt giới hạn 10 MB nhưng giải mã ra cả GB RAM trong process API.
MAX_PIXELS = 25_000_000


class InvalidImage(Exception):
    """Không phải ảnh, hoặc định dạng không hỗ trợ."""


@dataclass(frozen=True)
class Prepared:
    data_url: str
    width: int
    height: int


def _open(data: bytes) -> Image.Image:
    """Mở ảnh chỉ đọc header (chưa giải mã), kiểm định dạng và trần điểm ảnh."""
    try:
        img = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise InvalidImage("không đọc được ảnh") from exc
    if img.format not in ALLOWED_FORMATS:
        raise InvalidImage(f"định dạng {img.format} không hỗ trợ")
    if img.width * img.height > MAX_PIXELS:
        raise InvalidImage("ảnh quá nhiều điểm ảnh")
    return img


def validate(data: bytes) -> None:
    """Kiểm nhanh lúc nhận request: là ảnh, đúng định dạng, không quá trần điểm
    ảnh. Không giải mã toàn bộ: phần đó consumer làm trong `prepare`."""
    _open(data)


def load(data: bytes) -> Image.Image:
    """Giải mã, xoay theo EXIF, chuyển RGB. Ảnh trả về không mang EXIF (vị trí
    GPS, máy chụp) vì không truyền `exif=` khi lưu lại."""
    img = _open(data)
    try:
        img.load()
    except (OSError, Image.DecompressionBombError) as exc:
        raise InvalidImage("không đọc được ảnh") from exc
    return ImageOps.exif_transpose(img).convert("RGB")


def crop_borders(img: Image.Image, max_ratio: float) -> Image.Image:
    """Bỏ vùng nền gần đồng màu quanh tờ giấy (mặt bàn, khoảng trống). Mỗi cạnh
    cắt tối đa `max_ratio` kích thước, để không mất chữ sát mép khi nền và giấy
    gần màu nhau."""
    width, height = img.size
    gray = img.convert("L")
    corners = sorted(
        gray.getpixel(p) for p in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1))
    )
    background = (corners[1] + corners[2]) // 2
    mask = gray.point(lambda v: 255 if abs(v - background) > BORDER_TOLERANCE else 0)
    box = mask.getbbox()
    if box is None:
        return img
    left, top, right, bottom = box
    max_x, max_y = int(width * max_ratio), int(height * max_ratio)
    left, top = min(left, max_x), min(top, max_y)
    right, bottom = max(right, width - max_x), max(bottom, height - max_y)
    if (left, top, right, bottom) == (0, 0, width, height):
        return img
    return img.crop((left, top, right, bottom))


def resize(img: Image.Image, max_side: int) -> Image.Image:
    """Thu nhỏ giữ tỉ lệ cho cạnh dài nhất ≤ max_side. Không phóng to."""
    width, height = img.size
    longest = max(width, height)
    if longest <= max_side:
        return img
    scale = max_side / longest
    return img.resize((round(width * scale), round(height * scale)), Image.LANCZOS)


def to_data_url(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def prepare(data: bytes, max_side: int, max_crop_ratio: float) -> Prepared:
    img = resize(crop_borders(load(data), max_crop_ratio), max_side)
    return Prepared(data_url=to_data_url(img), width=img.width, height=img.height)
