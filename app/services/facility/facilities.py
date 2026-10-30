"""
Bảng cơ sở khám (Supabase `facilities`), cache trong bộ nhớ.

~1.800 dòng, đọc công khai bằng anon key. Mỗi FACILITY_REFRESH_SECONDS hỏi
`max(updated_at)`; đổi thì kéo lại cả bảng. Supabase lỗi thì dùng bản đang có;
chưa từng có bản nào thì báo FacilitiesUnavailable (503).

Dữ liệu DB còn sai (KAN-115, facility_data_issues.json), nên mỗi dòng đi qua
`clean()` trước khi dùng: sửa theo facility_overrides.json, rồi vài luật theo
tên làm lưới an toàn. Luật nào cũng chỉ được BỚT chỗ cơ sở có thể xuất hiện,
không bao giờ thêm — thà thiếu còn hơn sai.
"""

import asyncio
import logging
import time
import unicodedata
from dataclasses import dataclass

import httpx

from app import metrics
from app.config import settings
from app.services.facility import catalog

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000
_COLUMNS = "id,name,lat,lng,city,address,brand_kind,specialties,is_general,updated_at"


class FacilitiesUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Facility:
    id: str
    name: str
    lat: float
    lng: float
    specialties: frozenset[str]
    is_general: bool
    audience: str  # child | adult | all
    city: str | None = None
    address: str | None = None

    @property
    def pediatric(self) -> bool:
        return self.audience == "child" or "nhi" in self.specialties


def _words(name: str) -> list[str]:
    # Giữ dấu: "nhi" (nhi khoa) khác "nhị", bỏ dấu là nhầm.
    text = unicodedata.normalize("NFC", name).lower()
    return "".join(ch if ch.isalnum() else " " for ch in text).split()


def _has(words: list[str], phrase: str) -> bool:
    target = phrase.split()
    n = len(target)
    return any(words[i:i + n] == target for i in range(len(words) - n + 1))


def clean(row: dict) -> Facility | None:
    """Một dòng DB → Facility đã sửa, hoặc None nếu không được gợi ý."""
    fix = catalog.overrides().get(str(row.get("id")), {})
    if fix.get("exclude"):
        return None
    try:
        lat, lng = float(row["lat"]), float(row["lng"])
    except (KeyError, TypeError, ValueError):
        return None

    name = (row.get("name") or "").strip()
    known = catalog.specialties()
    specialties = set(fix["specialties"] if "specialties" in fix else (row.get("specialties") or []))
    specialties &= known.keys()
    is_general = bool(fix.get("is_general", row.get("is_general")))
    audience = fix.get("audience") or row.get("audience") or "all"

    words = _words(name)
    if _has(words, "pháp y"):
        return None
    if _has(words, "tâm thần"):
        # BV Tâm thần không phải Nội thần kinh, cũng không phải đa khoa.
        specialties.discard("than_kinh")
        specialties.add("tam_than")
        is_general = False
    if _has(words, "xét nghiệm"):
        is_general = False
    if audience == "all" and (_has(words, "nhi") or _has(words, "trẻ em")) and not _has(words, "sản"):
        # BV / phòng khám Nhi chỉ khám trẻ em; BV Sản–Nhi thì khám cả hai.
        audience = "child"

    if not is_general and not specialties:
        # Chưa gắn gì: chưa đủ dữ liệu để gợi ý.
        return None
    return Facility(
        id=str(row["id"]),
        name=name,
        lat=lat,
        lng=lng,
        specialties=frozenset(specialties),
        is_general=is_general,
        audience=audience,
        city=row.get("city") or None,
        address=row.get("address") or None,
    )


@dataclass(frozen=True)
class Snapshot:
    facilities: tuple[Facility, ...]
    version: str
    checked_at: float


_snapshot: Snapshot | None = None
_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10.0)
    return _client


def set_http_client(client: httpx.AsyncClient | None) -> None:
    """Dùng cho test."""
    global _client
    _client = client


def reset() -> None:
    """Dùng cho test."""
    global _snapshot
    _snapshot = None


async def stop_facility_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _url() -> str:
    return f"{settings.supabase_url.rstrip('/')}/rest/v1/{settings.facility_table}"


def _headers() -> dict:
    key = settings.supabase_anon_key
    return {"apikey": key, "Authorization": f"Bearer {key}"}


_FILTERS = {"category": "eq.hospital", "is_active": "eq.true"}


async def _get(params: dict) -> list[dict]:
    try:
        resp = await _http().get(_url(), params={**_FILTERS, **params}, headers=_headers())
    except httpx.HTTPError as exc:
        raise FacilitiesUnavailable(str(exc)) from exc
    if resp.status_code != 200:
        logger.error("Đọc %s lỗi HTTP %s: %s", settings.facility_table, resp.status_code, resp.text[:300])
        raise FacilitiesUnavailable(f"HTTP {resp.status_code}")
    return resp.json()


async def _latest_version() -> str:
    rows = await _get({"select": "updated_at", "order": "updated_at.desc", "limit": "1"})
    return str(rows[0]["updated_at"]) if rows else ""


async def _fetch_all() -> list[dict]:
    columns = _COLUMNS + (",audience" if settings.facility_audience_column else "")
    rows: list[dict] = []
    offset = 0
    while True:
        page = await _get({"select": columns, "order": "id", "limit": str(PAGE_SIZE), "offset": str(offset)})
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


async def _refresh(current: Snapshot | None) -> Snapshot:
    version = await _latest_version()
    now = time.monotonic()
    if current is not None and version == current.version:
        return Snapshot(current.facilities, current.version, now)
    rows = await _fetch_all()
    cleaned = tuple(f for f in (clean(r) for r in rows) if f is not None)
    metrics.incr("facility_reloads")
    logger.info("Nạp %d/%d cơ sở khám (updated_at=%s)", len(cleaned), len(rows), version)
    return Snapshot(cleaned, version, now)


async def get() -> Snapshot:
    global _snapshot
    current = _snapshot
    if current is not None and time.monotonic() - current.checked_at < settings.facility_refresh_seconds:
        return current
    async with _lock:
        current = _snapshot
        if current is not None and time.monotonic() - current.checked_at < settings.facility_refresh_seconds:
            return current
        try:
            _snapshot = await _refresh(current)
        except FacilitiesUnavailable:
            if current is None:
                raise
            metrics.incr("facility_stale")
            logger.warning("Không làm mới được bảng cơ sở, dùng bản cũ (updated_at=%s)", current.version)
            # Thử lại sau một chu kỳ, không phải ở mọi request.
            _snapshot = Snapshot(current.facilities, current.version, time.monotonic())
        return _snapshot
