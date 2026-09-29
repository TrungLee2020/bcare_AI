import json
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from app.config import settings

STATUS_PENDING = "pending"


def _claim_key(request_id: UUID | str) -> str:
    return f"dedup:{request_id}"


def _response_key(request_id: UUID | str) -> str:
    return f"resp:{request_id}"


async def claim(redis: Redis, request_id: UUID | str) -> bool:
    """
    Giữ chỗ cho request_id ở tầng API. Trả về True nếu đây là lần đầu thấy
    request_id này (được phép trừ quota + enqueue), False nếu là request trùng.

    SET NX là atomic nên 2 request trùng gửi song song (client retry khi mạng
    chập chờn) chỉ có đúng 1 cái thắng.
    """
    created = await redis.set(
        _claim_key(request_id),
        STATUS_PENDING,
        nx=True,
        ex=settings.dedup_ttl_seconds,
    )
    return bool(created)


async def release(redis: Redis, request_id: UUID | str) -> None:
    """
    Nhả chỗ đã giữ. Chỉ dùng khi request KHÔNG thực sự được đưa vào pipeline
    (hết quota, enqueue lỗi) — nếu không nhả, client retry cùng request_id sau
    khi quota reset sẽ bị coi là trùng và không bao giờ được xử lý.
    """
    await redis.delete(_claim_key(request_id))


async def get_cached_response(redis: Redis, request_id: UUID | str) -> dict[str, Any] | None:
    raw = await redis.get(_response_key(request_id))
    return json.loads(raw) if raw else None


async def save_response(redis: Redis, request_id: UUID | str, payload: dict[str, Any]) -> None:
    await redis.set(
        _response_key(request_id),
        json.dumps(payload, ensure_ascii=False),
        ex=settings.dedup_ttl_seconds,
    )
