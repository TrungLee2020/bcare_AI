"""
Kiểm đồng ý riêng cho AI (`user_consents.ai_chat_version` / `ai_share_profile`
trên Supabase của app) TRƯỚC khi trả lời.

App cũng tự kiểm, nhưng app không phải hàng rào: ai có access token đều gọi
thẳng được API này. Đọc qua PostgREST bằng service role key — bảng có RLS, key
thường của user chỉ đọc được dòng của chính họ và không dùng được ở server.

Kết quả cache ngắn trong Redis (CONSENT_CACHE_SECONDS): mỗi câu hỏi một lần gọi
Supabase là thừa, mà cache lâu thì người dùng rút lại đồng ý vẫn bị gửi dữ liệu
sang OpenAI thêm một lúc.
"""

import json
import logging
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis

from app import metrics
from app.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Consent:
    ai_chat: bool
    share_profile: bool


GRANTED_ALL = Consent(ai_chat=True, share_profile=True)


class ConsentUnavailable(Exception):
    """Không đọc được bảng đồng ý. Không được coi là "đã đồng ý" (fail-closed)."""


_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=5.0)
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Dùng cho test."""
    global _client
    _client = client


async def stop_consent_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _key(user_id: str) -> str:
    return f"consent:{user_id}"


def _evaluate(row: dict | None) -> Consent:
    if not row:
        return Consent(ai_chat=False, share_profile=False)
    version = row.get("ai_chat_version")
    if version is None or str(version).strip() == "":
        ai_chat = False
    elif settings.consent_required_version:
        ai_chat = str(version) == settings.consent_required_version
    else:
        ai_chat = True
    # Đồng ý chia sẻ hồ sơ không có nghĩa gì nếu chưa đồng ý dùng AI.
    return Consent(ai_chat=ai_chat, share_profile=ai_chat and row.get("ai_share_profile") is True)


async def _fetch(user_id: str) -> dict | None:
    url = f"{settings.supabase_url.rstrip('/')}/rest/v1/{settings.consent_table}"
    key = settings.supabase_service_role_key
    try:
        resp = await _http().get(
            url,
            params={
                settings.consent_user_column: f"eq.{user_id}",
                "select": "ai_chat_version,ai_share_profile",
                "limit": "1",
            },
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
    except httpx.HTTPError as exc:
        raise ConsentUnavailable(str(exc)) from exc
    if resp.status_code != 200:
        # 404/42P01 = bảng chưa có (migration chưa deploy), 401 = sai key...
        # Đều là lỗi cấu hình phía mình, không phải "user chưa đồng ý".
        logger.error("Đọc %s lỗi HTTP %s: %s", settings.consent_table, resp.status_code, resp.text[:300])
        raise ConsentUnavailable(f"HTTP {resp.status_code}")
    rows = resp.json()
    return rows[0] if rows else None


async def check(redis: Redis, user_id: str) -> Consent:
    if not settings.consent_required:
        return GRANTED_ALL

    cached = await redis.get(_key(user_id))
    if cached:
        data = json.loads(cached)
        return Consent(ai_chat=data["ai_chat"], share_profile=data["share_profile"])

    metrics.incr("consent_lookups")
    consent = _evaluate(await _fetch(user_id))
    await redis.set(
        _key(user_id),
        json.dumps({"ai_chat": consent.ai_chat, "share_profile": consent.share_profile}),
        ex=settings.consent_cache_seconds,
    )
    return consent
