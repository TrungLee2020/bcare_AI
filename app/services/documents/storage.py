"""
Tải / xoá ảnh giấy tờ trên Supabase Storage (bucket riêng tư, RLS theo user).

Dùng anon key + access token của chính user, như đọc user_consents: RLS chỉ
cho user đụng tới thư mục `{user_id}/` của mình, nên backend không giữ key
nào vượt RLS. Hệ quả: chỉ làm được khi đang có token user (lúc nhận request);
job chạy nền không có token thì không xoá được ảnh.
"""

import re

import httpx

from app.config import settings


class StorageError(Exception):
    """Lỗi mạng hoặc lỗi phía Storage: app thử lại sau."""


class ObjectNotFound(StorageError):
    pass


class ObjectTooLarge(StorageError):
    pass


_client: httpx.AsyncClient | None = None
_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Dùng cho test."""
    global _client
    _client = client


async def stop_storage_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def is_owned(path: str, user_id: str) -> bool:
    """Đường dẫn đúng dạng `{user_id}/{tên file}`: một cấp, không `..`. RLS
    cũng chặn, đây là lớp thứ nhất và cho mã lỗi rõ ràng."""
    prefix, sep, name = path.partition("/")
    return bool(sep) and prefix == user_id and name not in (".", "..") and bool(_NAME_RE.fullmatch(name))


def _headers(access_token: str) -> dict:
    return {"apikey": settings.supabase_anon_key, "Authorization": f"Bearer {access_token}"}


def _base() -> str:
    return settings.supabase_url.rstrip("/") + "/storage/v1/object"


async def download(path: str, access_token: str) -> bytes:
    url = f"{_base()}/authenticated/{settings.storage_bucket}/{path}"
    chunks: list[bytes] = []
    size = 0
    try:
        async with _http().stream("GET", url, headers=_headers(access_token)) as resp:
            # Supabase trả 400 (không phải 404) cho object không có / không được đọc.
            if resp.status_code in (400, 404):
                raise ObjectNotFound(path)
            if resp.status_code != 200:
                raise StorageError(f"HTTP {resp.status_code}")
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > settings.ocr_max_image_bytes:
                    raise ObjectTooLarge(f"> {settings.ocr_max_image_bytes} bytes")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise StorageError(str(exc)) from exc
    return b"".join(chunks)


def _is_not_found(resp: httpx.Response) -> bool:
    """Supabase trả 400 cho cả "không có object" lẫn bị RLS chặn; chỉ body mới
    phân biệt được (`statusCode` "404" / `error` "not_found")."""
    try:
        body = resp.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    error = str(body.get("error", "")).strip().lower()
    return str(body.get("statusCode", "")) == "404" or error in ("not_found", "not found")


async def delete(path: str, access_token: str) -> None:
    """Không có object (đã xoá rồi) coi như xong. 400 vì bị chặn (RLS, token
    sai) là lỗi: coi là xong thì API báo đã xoá trong khi ảnh vẫn còn."""
    url = f"{_base()}/{settings.storage_bucket}/{path}"
    try:
        resp = await _http().delete(url, headers=_headers(access_token))
    except httpx.HTTPError as exc:
        raise StorageError(str(exc)) from exc
    if resp.status_code in (200, 204, 404):
        return
    if resp.status_code == 400 and _is_not_found(resp):
        return
    raise StorageError(f"HTTP {resp.status_code}")
