"""
Xác thực request. Nhận hai loại token:

- **Access token Supabase** (JWT, 3 phần) — loại app mobile gửi. `sub` (UUID của
  auth.users) là user_id, gói lấy từ `app_metadata.tier`. Verify bằng khoá công
  khai JWKS của project (`SUPABASE_URL`) hoặc JWT secret kiểu cũ HS256
  (`SUPABASE_JWT_SECRET`), tuỳ project đang ký bằng loại nào.
- **Token HMAC cũ** (2 phần, `AUTH_SECRET`) — giữ cho `/chat/*` và công cụ dev.

Vì sao cần, ngoài chuyện "bảo mật chung chung": trước Phase 6 `user_id` và
`tier` được lấy từ **body/query do client gửi**. Nghĩa là bất kỳ ai cũng có thể
gửi `tier: "premium"` để được 5 câu/ngày thay vì 2, và mở SSE bằng `user_id` của
người khác để nghe câu trả lời sức khoẻ của họ. Từ giờ cả hai trường đó chỉ đến
từ token đã ký, client gửi gì trong body cũng không còn ý nghĩa.
"""

import asyncio
import base64
import hmac
import json
import logging
import time
from dataclasses import dataclass
from hashlib import sha256

import jwt
from fastapi import HTTPException, Query, Request, status

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    user_id: str
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


def issue_token(user_id: int | str, tier: str, ttl_seconds: int = 3600) -> str:
    """Chỉ dùng cho test và công cụ dev. Production thì BE phát token."""
    payload = json.dumps(
        {"user_id": user_id, "tier": tier, "exp": int(time.time()) + ttl_seconds},
        separators=(",", ":"),
    ).encode()
    return f"{_b64encode(payload)}.{_sign(payload)}"


def verify_token(token: str) -> Principal:
    """Token HMAC cũ (`AUTH_SECRET`)."""
    if not settings.auth_secret:
        # Không có secret thì _sign() ký bằng chuỗi rỗng — ai cũng tự ký được.
        raise AuthError("Loại token này không được bật")
    try:
        encoded_payload, signature = token.split(".", 1)
        payload = _b64decode(encoded_payload)
    except (ValueError, TypeError) as exc:
        raise AuthError("Token sai định dạng") from exc

    # compare_digest chứ không phải ==: so sánh chuỗi thường thoát ra sớm ở byte
    # đầu tiên khác nhau, để lộ thông tin qua thời gian phản hồi.
    # So bytes, không so str: compare_digest(str, str) ném TypeError khi chuỗi
    # có ký tự non-ASCII, thành 500 thay vì 401.
    if not hmac.compare_digest(signature.encode(), _sign(payload).encode()):
        raise AuthError("Chữ ký không hợp lệ")

    try:
        data = json.loads(payload)
        user_id = str(data["user_id"])
        tier = str(data["tier"])
        expires_at = int(data["exp"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise AuthError("Nội dung token không hợp lệ") from exc

    if expires_at < time.time():
        raise AuthError("Token đã hết hạn")
    if tier not in ("free", "premium"):
        raise AuthError("Tier không hợp lệ")

    return Principal(user_id=user_id, tier=tier)


# Chỉ nhận đúng các thuật toán này. Không có danh sách cố định thì token tự khai
# `alg: none`, hoặc `alg: HS256` ký bằng chính khoá CÔNG KHAI, cũng lọt.
_JWKS_ALGORITHMS = ["ES256", "RS256"]
_SECRET_ALGORITHMS = ["HS256"]

_jwks_client: jwt.PyJWKClient | None = None


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(
            f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json",
            cache_keys=True,
            lifespan=settings.supabase_jwks_cache_seconds,
            timeout=5,
        )
    return _jwks_client


def set_jwks_client(client) -> None:
    """Dùng cho test."""
    global _jwks_client
    _jwks_client = client


def supabase_enabled() -> bool:
    return bool(settings.supabase_url or settings.supabase_jwt_secret)


def _tier_from_claims(claims: dict) -> str:
    """Gói lấy từ `app_metadata` — phần CHỈ server (service role) ghi được.
    `user_metadata` thì user tự sửa được bằng chính access token của mình, đọc
    gói từ đó là tự cấp premium."""
    app_metadata = claims.get("app_metadata") or {}
    tier = app_metadata.get("tier") if isinstance(app_metadata, dict) else None
    return "premium" if tier == "premium" else "free"


async def verify_supabase_token(token: str) -> Principal:
    if not supabase_enabled():
        raise AuthError("Loại token này không được bật")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AuthError("Token sai định dạng") from exc

    alg = header.get("alg")
    if alg in _SECRET_ALGORITHMS and settings.supabase_jwt_secret:
        key, algorithms = settings.supabase_jwt_secret, _SECRET_ALGORITHMS
    elif alg in _JWKS_ALGORITHMS and settings.supabase_url:
        try:
            # Lần đầu (và khi gặp `kid` lạ, tức project vừa xoay khoá) phải tải
            # JWKS qua mạng; PyJWKClient dùng urllib đồng bộ nên đẩy sang thread.
            signing_key = await asyncio.to_thread(_jwks().get_signing_key_from_jwt, token)
        except jwt.PyJWKClientConnectionError as exc:
            logger.exception("Không tải được JWKS của Supabase")
            raise AuthUnavailable() from exc
        except jwt.PyJWTError as exc:
            raise AuthError("Không tìm thấy khoá ký của token") from exc
        key, algorithms = signing_key.key, _JWKS_ALGORITHMS
    else:
        raise AuthError("Thuật toán ký không được chấp nhận")

    issuer = (
        f"{settings.supabase_url.rstrip('/')}/auth/v1" if settings.supabase_url else None
    )
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=settings.supabase_jwt_audience,
            issuer=issuer,
            options={"require": ["exp", "sub", "aud"]},
            leeway=30,
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Token đã hết hạn") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Token không hợp lệ") from exc

    # anon key cũng là một JWT hợp lệ của project (role=anon, không có user).
    if claims.get("role") != "authenticated":
        raise AuthError("Token không phải của người dùng đã đăng nhập")
    return Principal(user_id=str(claims["sub"]), tier=_tier_from_claims(claims))


class AuthUnavailable(Exception):
    """Không kiểm được token vì lỗi phía mình (vd không tải được JWKS) — không
    phải lỗi của token, nên không được trả 401 (app sẽ đăng xuất người dùng)."""


async def authenticate(token: str) -> Principal:
    # JWT có đúng 3 phần, token HMAC cũ có 2.
    if token.count(".") == 2:
        return await verify_supabase_token(token)
    return verify_token(token)


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
            user_id=request.query_params.get("user_id", "0"),
            tier=request.query_params.get("tier", "free"),
        )

    raw = _extract_token(request, token)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Thiếu token"
        )
    try:
        return await authenticate(raw)
    except AuthUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Chưa xác thực được phiên đăng nhập, vui lòng thử lại.",
        ) from exc
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
