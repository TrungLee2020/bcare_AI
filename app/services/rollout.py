"""
Cổng bật/tắt tính năng theo nhóm user, phục vụ rollout dần.

Bật cho toàn bộ user ngay từ đầu là canh bạc: chi phí OpenAI thực tế chưa được
đo, chất lượng trả lời tiếng Việt chưa được kiểm trên người dùng thật. Cổng này
cho phép mở cho một nhóm nhỏ, theo dõi, rồi tăng dần — và tắt ngay bằng biến môi
trường nếu có chuyện.
"""

import logging
from hashlib import sha256

from app.config import settings

logger = logging.getLogger(__name__)


def allowlist() -> set[int]:
    raw = settings.rollout_allowlist.strip()
    if not raw:
        return set()
    return {int(part) for part in raw.split(",") if part.strip()}


def bucket_of(user_id: int) -> int:
    """
    Chia user vào 100 nhóm, ổn định theo user_id.

    Dùng hash chứ không dùng `user_id % 100`: user_id thường được cấp tuần tự
    nên chia lấy dư sẽ gom user đăng ký cùng đợt vào cùng nhóm, mẫu rollout mất
    tính đại diện. Phải ổn định (không random) để một user không lúc được lúc
    không giữa các request.
    """
    digest = sha256(str(user_id).encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


def is_enabled(user_id: int) -> bool:
    if not settings.rollout_enabled:
        return False
    if user_id in allowlist():
        return True
    return bucket_of(user_id) < settings.rollout_percentage
