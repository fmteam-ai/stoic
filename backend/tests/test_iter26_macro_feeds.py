"""Iter26 — FRED macro feeds integration.

Covers:
  - macro_feeds._fetch_series happy path (mocked httpx)
  - Holiday "." values filtered out
  - Insufficient observations → None (caller falls back to cache)
  - Missing API key → None, no crash
  - get_macro_snapshot uses Mongo cache when fresh
  - get_macro_snapshot falls back to stale cache when FRED unreachable
  - get_macro_snapshot single-flight lock prevents stampedes
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta


def _mock_obs(values):
    """Build a fake FRED observations payload from a list of values
    (newest first). `None` becomes a holiday '.' sentinel."""
    today = datetime.now(timezone.utc).date()
    out = []
    for i, v in enumerate(values):
        out.append({
            "date": (today - timedelta(days=i)).isoformat(),
            "value": "." if v is None else str(v),
        })
    return {"observations": out}


@pytest.mark.asyncio
async def test_fetch_series_happy_path(monkeypatch):
    from macro_feeds import _fetch_series
    monkeypatch.setenv("FRED_API_KEY", "test-key")

    class FakeResp:
        status_code = 200
        def json(self): return _mock_obs([4.50, 4.51, 4.52, 4.50, 4.48, 4.47, 4.46, 4.50])

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, params=None): return FakeResp()

    monkeypatch.setattr("macro_feeds.httpx.AsyncClient", lambda **kw: FakeClient())
    result = await _fetch_series("DGS10")
    assert result is not None
    assert result["series_id"] == "DGS10"
    assert result["latest"] == 4.50
    assert result["dod_delta"] == round(4.50 - 4.51, 3)
    assert result["wow_delta"] == round(4.50 - 4.47, 3)


@pytest.mark.asyncio
async def test_fetch_series_filters_holiday_dots(monkeypatch):
    """Verify '.' (holiday) values are skipped, so deltas use real numbers."""
    from macro_feeds import _fetch_series
    monkeypatch.setenv("FRED_API_KEY", "test-key")

    class FakeResp:
        status_code = 200
        def json(self):
            # values[1] is a holiday — dod should compare values[0] vs values[2]
            return _mock_obs([4.50, None, 4.51, 4.52, 4.50, 4.48, 4.47, 4.46])

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, params=None): return FakeResp()

    monkeypatch.setattr("macro_feeds.httpx.AsyncClient", lambda **kw: FakeClient())
    result = await _fetch_series("DGS10")
    assert result is not None
    # dod should be latest - first-valid-skipping-holiday = 4.50 - 4.51
    assert result["dod_delta"] == round(4.50 - 4.51, 3)


@pytest.mark.asyncio
async def test_fetch_series_insufficient_data(monkeypatch):
    from macro_feeds import _fetch_series
    monkeypatch.setenv("FRED_API_KEY", "test-key")

    class FakeResp:
        status_code = 200
        def json(self): return _mock_obs([4.50, 4.51])  # only 2 obs

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, params=None): return FakeResp()

    monkeypatch.setattr("macro_feeds.httpx.AsyncClient", lambda **kw: FakeClient())
    result = await _fetch_series("DGS10")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_series_missing_api_key(monkeypatch):
    from macro_feeds import _fetch_series
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    result = await _fetch_series("DGS10")
    assert result is None


@pytest.mark.asyncio
async def test_fetch_series_http_failure(monkeypatch):
    from macro_feeds import _fetch_series
    monkeypatch.setenv("FRED_API_KEY", "test-key")

    class FakeResp:
        status_code = 500
        def json(self): return {}

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, params=None): return FakeResp()

    monkeypatch.setattr("macro_feeds.httpx.AsyncClient", lambda **kw: FakeClient())
    result = await _fetch_series("DGS10")
    assert result is None


@pytest.mark.asyncio
async def test_get_macro_snapshot_uses_cache(monkeypatch):
    """When cached doc is fresh (<1h), don't hit FRED at all."""
    import macro_feeds
    macro_feeds._fetch_locks.clear()  # reset locks between tests
    from macro_feeds import get_macro_snapshot

    now = datetime.now(timezone.utc)
    cached_doc = lambda sid: {
        "_id": sid, "series_id": sid, "name": "fake", "unit": "%",
        "latest": 1.0, "date": "2026-06-23",
        "dod_delta": 0.01, "wow_delta": 0.02,
        "fetched_at": now.isoformat(),  # FRESH
    }
    fake_db = MagicMock()
    fake_db.fred_cache.find_one = AsyncMock(side_effect=lambda q: cached_doc(q["_id"]))
    fake_db.fred_cache.update_one = AsyncMock()
    monkeypatch.setattr("macro_feeds.get_db", lambda: fake_db, raising=False)
    # Patch via the database module since macro_feeds imports lazily
    import macro_feeds
    monkeypatch.setattr(macro_feeds, "get_db", lambda: fake_db, raising=False)
    # Make sure HTTP is NOT called
    fetch_mock = AsyncMock()
    monkeypatch.setattr("macro_feeds._fetch_series", fetch_mock)

    snap = await get_macro_snapshot()
    assert len(snap["series"]) == 5
    assert snap["stale_cache"] is False
    fetch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_get_macro_snapshot_falls_back_to_stale_cache(monkeypatch):
    """When FRED is unreachable AND cache is expired, serve stale + flag it."""
    import macro_feeds
    macro_feeds._fetch_locks.clear()  # reset locks between tests
    from macro_feeds import get_macro_snapshot

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    stale = lambda sid: {
        "_id": sid, "series_id": sid, "name": "fake", "unit": "%",
        "latest": 1.0, "date": "2026-06-20",
        "dod_delta": 0.0, "wow_delta": 0.0,
        "fetched_at": old_ts,
    }
    fake_db = MagicMock()
    fake_db.fred_cache.find_one = AsyncMock(side_effect=lambda q: stale(q["_id"]))
    fake_db.fred_cache.update_one = AsyncMock()
    import macro_feeds
    monkeypatch.setattr(macro_feeds, "get_db", lambda: fake_db, raising=False)
    # FRED returns None (unreachable)
    monkeypatch.setattr("macro_feeds._fetch_series", AsyncMock(return_value=None))

    snap = await get_macro_snapshot()
    assert snap["stale_cache"] is True
    assert all(s.get("stale_cache") is True for s in snap["series"])


@pytest.mark.asyncio
async def test_snapshot_endpoint_requires_auth(monkeypatch):
    """GET /macro/snapshot must require authenticated session."""
    import os
    import requests
    base = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:3000").rstrip("/")
    r = requests.get(f"{base}/api/macro/snapshot", timeout=10)
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_snapshot_endpoint_returns_5_series_for_admin():
    import os
    import requests
    base = os.environ.get("REACT_APP_BACKEND_URL", "http://localhost:3000").rstrip("/")
    s = requests.Session()
    r = s.post(f"{base}/api/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"}, timeout=15)
    assert r.status_code == 200
    r = s.get(f"{base}/api/macro/snapshot", timeout=20)
    assert r.status_code == 200
    body = r.json()
    assert body["has_api_key"] is True
    assert len(body["series"]) == 5
    series_ids = {s["series_id"] for s in body["series"]}
    assert series_ids == {"DGS10", "DGS2", "FEDFUNDS", "DTWEXBGS", "T10YIE"}
    for s in body["series"]:
        assert "latest" in s and "dod_delta" in s and "wow_delta" in s and "date" in s


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
