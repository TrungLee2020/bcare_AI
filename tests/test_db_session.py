from app.db import session


async def test_engine_hides_sql_parameters_in_errors(monkeypatch):
    seen = {}

    def fake_engine(url, **kwargs):
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(session, "create_async_engine", fake_engine)
    monkeypatch.setattr(session, "async_sessionmaker", lambda *a, **k: None)
    monkeypatch.setattr(session, "_engine", None)
    monkeypatch.setattr(session, "_sessionmaker", None)
    await session.start_db()
    assert seen["hide_parameters"] is True
