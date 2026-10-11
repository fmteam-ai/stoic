"""HTTP contract for the funding-carry, strategy-DSL and prediction-track routes,
plus the observe-by-default wiring of the chart-pattern gate."""
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


@pytest.fixture
def carry(monkeypatch):
    from crypto_bridge import funding_carry as fc
    from routes import crypto_routes as cr
    db = FakeDb()
    monkeypatch.setattr(cr, "get_db", lambda: db)
    monkeypatch.setattr(fc, "_public_exchange", lambda eid: _FakeExchange())
    fc._SCAN_CACHE.clear()
    return _app(cr.router), db


@pytest.mark.asyncio
async def test_funding_carry_scan_hides_snapshots_and_validates(carry):
    app, _ = carry
    async with _client(app) as c:
        r = await c.post("/crypto/funding-carry/scan", json={"bases": ["btc", "BTC", "eth"]})
        assert r.status_code == 200
        body = r.json()
        assert "snapshots" not in body and body["mode"] == "shadow"
        assert [o["base"] for o in body["opportunities"]] == ["BTC", "ETH"]           # upper-cased, de-duplicated
        assert (await c.post("/crypto/funding-carry/scan", json={"bases": ["../x"]})).status_code == 422
        assert (await c.post("/crypto/funding-carry/scan", json={"exchange_id": "kraken"})).status_code == 422
        assert (await c.post("/crypto/funding-carry/scan", json={"bases": ["A1"] * 11})).status_code == 422


@pytest.mark.asyncio
async def test_funding_carry_shadow_cycle_and_listing_are_per_user(carry):
    app, db = carry
    async with _client(app) as c:
        r = await c.post("/crypto/funding-carry/shadow/cycle",
                         json={"bases": ["BTC"], "config": {"leverage": 0, "capital": 5000}})
        assert r.status_code == 200
        assert [e["action"] for e in r.json()["events"]] == ["open"]
        row = db.funding_carry_shadow.rows[0]
        assert row["user_id"] == "u1" and row["mode"] == "shadow" and row["leverage"] == 1.0   # clamped
        db.funding_carry_shadow.rows.append({**row, "_id": None, "user_id": "intruder"})
        listing = (await c.get("/crypto/funding-carry/shadow")).json()
        assert len(listing["positions"]) == 1 and isinstance(listing["positions"][0]["id"], str)
        assert listing["summary"]["open_positions"] == 1


# ───────────────────────── strategy DSL ─────────────────────────

DSL = {"version": "1.0", "name": "t", "symbols": ["XAUUSD"], "params": {"rsi_oversold": 30},
       "entry_rules": [{"field": "atr_pct", "op": ">", "value": 0, "side": "BUY"},
                       {"field": "confidence", "op": ">", "value": 70, "side": "ANY"}],
       "exit_rules": [{"kind": "take_profit_pct", "value": 0.3}, {"kind": "stop_loss_pct", "value": 0.3}],
       "pseudocode": ""}


@pytest.fixture
def nl(monkeypatch):
    from routes import nl_routes
    db = FakeDb()
    monkeypatch.setattr(nl_routes, "get_db", lambda: db)
    return _app(nl_routes.router), db


def _m15(n=200):
    import math
    return [{"t": 1_760_000_000 + i * 900, "o": 100 + math.sin(i / 3), "h": 100.6 + math.sin(i / 3),
             "l": 99.4 + math.sin(i / 3), "c": 100 + math.sin((i + 1) / 3)} for i in range(n)]


@pytest.mark.asyncio
async def test_explain_validates_and_reads_back(nl):
    app, _ = nl
    async with _client(app) as c:
        r = await c.post("/nl/strategy/explain", json={"dsl": DSL})
        assert r.status_code == 200
        assert "Buy at the next bar's open when ATR(14)" in r.json()["text"]
        assert "Live-only conditions" in r.json()["text"]
        bad = {**DSL, "entry_rules": [{"field": "__import__", "op": "<", "value": 1, "side": "BUY"}]}
        r = await c.post("/nl/strategy/explain", json={"dsl": bad})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "invalid_dsl"


@pytest.mark.asyncio
async def test_simulate_m15_uses_only_the_callers_candles(nl):
    app, db = nl
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "bars": _m15()})
    async with _client(app) as c:
        r = await c.post("/nl/strategy/simulate", json={"dsl": DSL, "timeframe": "M15", "cost_bps": 1})
        assert r.status_code == 200
        body = r.json()
        assert body["ready"] and body["symbol"] == "XAUUSD" and body["result"]["trades_n"] > 0
        assert any("Partial backtest" in n for n in body["notes"])
        db.intraday_candles.rows[0]["user_id"] = "someone_else"
        r = await c.post("/nl/strategy/simulate", json={"dsl": DSL})
        assert r.status_code == 200 and r.json()["ready"] is False


@pytest.mark.asyncio
async def test_simulate_rejects_bad_inputs(nl):
    app, _ = nl
    async with _client(app) as c:
        assert (await c.post("/nl/strategy/simulate", json={"dsl": DSL, "symbol": "BTCUSD"})).status_code == 422
        assert (await c.post("/nl/strategy/simulate", json={"dsl": DSL, "timeframe": "H4"})).status_code == 422
        assert (await c.post("/nl/strategy/simulate", json={"dsl": DSL, "cost_bps": "x"})).status_code == 422


@pytest.mark.asyncio
async def test_simulate_daily_uses_market_history(nl, monkeypatch):
    import market
    app, _ = nl
    rows = [{"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": b["o"], "high": b["h"],
             "low": b["l"], "close": b["c"]} for i, b in enumerate(_m15(300))]

    async def _hist(symbol):
        return rows
    monkeypatch.setattr(market, "get_history", _hist)
    async with _client(app) as c:
        body = (await c.post("/nl/strategy/simulate", json={"dsl": DSL, "timeframe": "D1"})).json()
        assert body["timeframe"] == "D1" and body["ready"] and body["result"]["bars"] == 300


# ───────────────────────── prediction track ─────────────────────────

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
