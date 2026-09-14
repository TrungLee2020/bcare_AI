"""
Xác thực request bằng token do BE ký (HMAC-SHA256).

Vì sao cần, ngoài chuyện "bảo mật chung chung": trước phase này `user_id` và
`tier` được lấy từ **body/query do client gửi**. Nghĩa là bất kỳ ai cũng có thể
gửi `tier: "premium"` để được 5 câu/ngày thay vì 2, và mở SSE bằng `user_id` của
người khác để nghe câu trả lời sức khoẻ của họ. Từ giờ cả hai trường đó chỉ đến
từ token đã ký, client gửi gì trong body cũng không còn ý nghĩa.

Giả định: BE hiện có ký token bằng secret dùng chung. Nếu BE đã phát JWT sẵn thì
thay phần thân `verify_token` bằng bước verify JWT — phần còn lại của code chỉ
phụ thuộc vào `Principal`.
"""

import base64
import hmac
import json
import logging
import time
from dataclasses import dataclass
from hashlib import sha256

from fastapi import HTTPException, Query, Request, status

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    user_id: int
    tier: str


class AuthError(Exception):
    pass


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: bytes) -> str:
    return _b64encode(
        hmac.new(settings.auth_secret.encode(), payload, sha256).digest()
    )


def issue_token(user_id: int, tier: str, ttl_seconds: int = 3600) -> str:
    """Chỉ dùng cho test và công cụ dev. Production thì BE phát token."""
    payload = json.dumps(
        {"user_id": user_id, "tier": tier, "exp": int(time.time()) + ttl_seconds},
        separators=(",", ":"),
    ).encode()
    return f"{_b64encode(payload)}.{_sign(payload)}"


def verify_token(token: str) -> Principal:
    try:
        encoded_payload, signature = token.split(".", 1)
        payload = _b64decode(encoded_payload)
    except (ValueError, TypeError) as exc:
        raise AuthError("Token sai định dạng") from exc

    # compare_digest chứ không phải ==: so sánh chuỗi thường thoát ra sớm ở byte
    # đầu tiên khác nhau, để lộ thông tin qua thời gian phản hồi.
    if not hmac.compare_digest(signature, _sign(payload)):
        raise AuthError("Chữ ký không hợp lệ")

    try:
        data = json.loads(payload)
        user_id = int(data["user_id"])
        tier = str(data["tier"])
        expires_at = int(data["exp"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise AuthError("Nội dung token không hợp lệ") from exc

    if expires_at < time.time():
        raise AuthError("Token đã hết hạn")
    if tier not in ("free", "premium"):
        raise AuthError("Tier không hợp lệ")

    return Principal(user_id=user_id, tier=tier)


def _extract_token(request: Request, token_query: str | None) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # EventSource của trình duyệt KHÔNG gửi được header tuỳ ý, nên endpoint SSE
    # buộc phải nhận token qua query param. Đánh đổi: token sẽ nằm trong access
    # log của proxy — hãy đặt TTL ngắn cho loại token này.
    return token_query


async def current_principal(
    request: Request, token: str | None = Query(None, include_in_schema=False)
) -> Principal:
    if not settings.auth_required:
        # Chế độ dev. Đã cảnh báo to ở lúc khởi động (xem app/main.py).
        return Principal(
            user_id=int(request.query_params.get("user_id", 0)),
            tier=request.query_params.get("tier", "free"),
        )

    raw = _extract_token(request, token)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Thiếu token"
        )
    try:
        return verify_token(raw)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
