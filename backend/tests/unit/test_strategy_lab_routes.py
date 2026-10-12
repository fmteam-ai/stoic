"""HTTP contract for the prediction-track route, plus the observe-by-default
wiring of the chart-pattern gate."""
import os
import sys

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
pytestmark = pytest.mark.unit

from fake_mongo import FakeDb  # noqa: E402

USER = {"id": "u1", "role": "user", "email": "u@x"}
H8 = 8 * 3600 * 1000


def _app(router):
    from auth import get_current_user
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: USER
    return app


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# ───────────────────────── funding carry ─────────────────────────

class _FakeExchange:
    async def fetch_funding_rate(self, sym):
        return {"fundingRate": 0.0003, "markPrice": 100.02, "interval": "8h"}

    async def fetch_funding_rate_history(self, sym, since, limit):
        return [{"timestamp": i * H8, "fundingRate": 0.0003} for i in range(limit)]

    async def fetch_ticker(self, sym):
        return {"last": 100.0}

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_predictions_endpoint(monkeypatch):
    from routes import trade_routes
    db = FakeDb()
    monkeypatch.setattr(trade_routes, "get_db", lambda: db)
    async with _client(_app(trade_routes.router)) as c:
        assert (await c.get("/trades/predictions", params={"symbol": "x y"})).status_code == 422
        r = await c.get("/trades/predictions", params={"symbol": "XAUUSD"})
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "XAUUSD" and body["recent"] == [] and body["calibration"]["decided"] == 0


# ───────────────────────── gate wiring ─────────────────────────

def test_pattern_gate_is_observe_only_unless_enabled():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "bot_runner.py"), encoding="utf-8").read()
    block = src[src.index("from chart_patterns import"):src.index("# iter-129 · Session-trend")]
    assert '"would_veto": pg' in block
    assert 'if pg and cfg.get("pattern_gate_enabled", False):' in block
    assert block.count("continue") == 1                     # the only skip sits under the flag
