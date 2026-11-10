import httpx
import pytest

from app.config import settings
from app.services.documents import storage

USER = "8a1f6c2e-1111-4222-8333-944455556666"


@pytest.fixture(autouse=True)
def storage_settings(monkeypatch):
    monkeypatch.setattr(settings, "supabase_url", "https://proj.supabase.co")
    monkeypatch.setattr(settings, "supabase_anon_key", "anon")
    yield
    storage.set_http_client(None)


def use(handler):
    storage.set_http_client(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize(
    "path, owned",
    [
        (f"{USER}/abc.jpg", True),
        (f"{USER}/a-b_c.1.png", True),
        ("someone-else/abc.jpg", False),
        (f"{USER}/../x/abc.jpg", False),
        (f"{USER}/sub/abc.jpg", False),
        (f"{USER}/..", False),
        (f"{USER}/", False),
        ("abc.jpg", False),
    ],
)
def test_is_owned(path, owned):
    assert storage.is_owned(path, USER) is owned


async def test_download_uses_user_token_and_anon_key():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["apikey"] = request.headers["apikey"]
        return httpx.Response(200, content=b"img")

    use(handler)
    assert await storage.download(f"{USER}/a.jpg", "tok") == b"img"
    assert seen == {
        "url": f"https://proj.supabase.co/storage/v1/object/authenticated/health-documents/{USER}/a.jpg",
        "auth": "Bearer tok",
        "apikey": "anon",
    }


@pytest.mark.parametrize("status", [400, 404])
async def test_download_missing_object(status):
    use(lambda request: httpx.Response(status, json={"error": "not_found"}))
    with pytest.raises(storage.ObjectNotFound):
        await storage.download(f"{USER}/a.jpg", "tok")


async def test_download_server_error():
    use(lambda request: httpx.Response(500))
    with pytest.raises(storage.StorageError):
        await storage.download(f"{USER}/a.jpg", "tok")


async def test_download_stops_when_too_large(monkeypatch):
    monkeypatch.setattr(settings, "ocr_max_image_bytes", 10)
    use(lambda request: httpx.Response(200, content=b"x" * 11))
    with pytest.raises(storage.ObjectTooLarge):
        await storage.download(f"{USER}/a.jpg", "tok")


async def test_download_network_error():
    def handler(request):
        raise httpx.ConnectError("down")

    use(handler)
    with pytest.raises(storage.StorageError):
        await storage.download(f"{USER}/a.jpg", "tok")


async def test_delete_ignores_missing_object():
    calls = []

    def handler(request):
        calls.append((request.method, str(request.url)))
        return httpx.Response(404)

    use(handler)
    await storage.delete(f"{USER}/a.jpg", "tok")
    assert calls == [("DELETE", f"https://proj.supabase.co/storage/v1/object/health-documents/{USER}/a.jpg")]


async def test_delete_server_error():
    use(lambda request: httpx.Response(500))
    with pytest.raises(storage.StorageError):
        await storage.delete(f"{USER}/a.jpg", "tok")


@pytest.mark.parametrize(
    "body",
    [
        {"statusCode": "404", "error": "not_found", "message": "Object not found"},
        {"statusCode": "404", "error": "Not found", "message": "Object not found"},
        {"error": "not_found"},
    ],
)
async def test_delete_400_not_found_counts_as_deleted(body):
    use(lambda request: httpx.Response(400, json=body))
    await storage.delete(f"{USER}/a.jpg", "tok")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(400, json={"statusCode": "403", "error": "Unauthorized", "message": "new row violates row-level security policy"}),
        httpx.Response(400, text="not json"),
    ],
)
async def test_delete_400_denied_is_an_error(response):
    use(lambda request: response)
    with pytest.raises(storage.StorageError):
        await storage.delete(f"{USER}/a.jpg", "tok")
