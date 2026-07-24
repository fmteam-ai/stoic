"""iter-91 — Phase 4 unified regime detection + regime-edge strategy gating."""
import asyncio
import math
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from market_regime import (_CACHE, _regime_key, detect, regime_gate,
                           sentiment_axis, strategy_edge, volatility_axis)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter91-{uuid.uuid4().hex[:8]}"


def _bars(n=120, rng=2.0, drift=0.0, last_rng=None, base=4000.0):
    now = time.time()
    out, px = [], base
    for i in range(n):
        px += drift
        r = last_rng if (last_rng is not None and i == n - 1) else rng
        out.append({"t": now - (n - 1 - i) * 900, "o": px, "h": px + r,
                    "l": px - r, "c": px, "v": 100})
    return out


async def _cleanup(db):
    await db.trades.delete_many({"user_id": UID})
    await db.intraday_candles.delete_many({"user_id": UID})
    _CACHE.pop(UID, None)


# --------------------------------------------------------------- axes
def test_volatility_axis():
    assert volatility_axis(_bars())["axis"] == "normal"
    calm = _bars(n=120, rng=2.0)
    for b in calm[-20:]:
        b["h"] = b["c"] + 8.0
        b["l"] = b["c"] - 8.0
    assert volatility_axis(calm)["axis"] == "high"
    assert volatility_axis(_bars(n=20))["ratio"] is None


def test_sentiment_axis():
    assert sentiment_axis({"crypto": 1.5, "gold": -0.5})["axis"] == "risk_on"
    assert sentiment_axis({"crypto": -1.2, "indices": -0.8, "gold": 1.0})["axis"] == "risk_off"
    assert sentiment_axis({"crypto": 0.1, "gold": 0.1})["axis"] == "neutral"
    assert sentiment_axis({"gold": 1.0})["axis"] == "neutral"  # no risk asset
    assert sentiment_axis({})["score"] is None


def test_regime_key():
    assert _regime_key("trending_up", "high", False) == "trending_up|high"
    assert _regime_key("ranging", "low", True) == "news_driven"


# --------------------------------------------------------------- detect
def test_detect_trending_and_axes(db):
    async def run():
        await _cleanup(db)
        await db.intraday_candles.insert_one(
            {"user_id": UID, "symbol": "XAUUSD", "timeframe": "M15",
             "bars": _bars(drift=0.5), "updated_at": datetime.now(timezone.utc)})
        await db.intraday_candles.insert_one(
            {"user_id": UID, "symbol": "BTCUSD", "timeframe": "M15",
             "bars": _bars(drift=40.0, rng=30.0, base=100000.0),
             "updated_at": datetime.now(timezone.utc)})
        r = await detect(db, UID, force=True)
        assert r["trend"]["axis"] == "trending_up"
        assert r["volatility"]["axis"] in ("high", "normal", "low")
        assert r["sentiment"]["axis"] in ("risk_on", "risk_off", "neutral")
        assert isinstance(r["news"]["news_driven"], bool)
        assert r["key"] and r["label"]
        assert r["primary_symbol"] == "XAUUSD"
        # cache: second call without force returns the same snapshot
        r2 = await detect(db, UID)
        assert r2["at"] == r["at"]
        await _cleanup(db)
    _run(run())


def test_detect_no_data_fails_open(db):
    async def run():
        await _cleanup(db)
        r = await detect(db, UID, force=True)
        assert r["trend"]["axis"] == "ranging"
        assert r["key"]
        await _cleanup(db)
    _run(run())


# ------------------------------------------------------------ edge gate
async def _seed_regime_trades(db, cls, key, pnls):
    for i, p in enumerate(pnls):
        await db.trades.insert_one({
            "user_id": UID, "origin": "auto", "status": "closed",
            "pnl": float(p), "strategy_class": cls,
            "market_regime": {"key": key, "label": key},
            "closed_at": (datetime.now(timezone.utc)
                          - timedelta(days=i % 20)).isoformat()})


def test_strategy_edge_blocks_proven_negative(db):
    async def run():
        await _cleanup(db)
        key = "ranging|high"
        await _seed_regime_trades(db, "scalp", key,
                                  [-30, -25, -40, -22, -35, -28, -31, -26, -38, -20])
        await _seed_regime_trades(db, "trend", key,
                                  [50, 60, -20, 45, 70, 55, -10, 65, 40, 52])
        edge = await strategy_edge(db, UID, key)
        assert edge["scalp"]["allowed"] is False
        assert "negative edge" in edge["scalp"]["reason"]
        assert edge["trend"]["allowed"] is True
        assert edge["breakout"]["allowed"] is True   # no history → fail-open
        assert edge["breakout"]["n"] == 0
        # different regime bucket → scalp allowed again
        edge2 = await strategy_edge(db, UID, "trending_up|low")
        assert edge2["scalp"]["allowed"] is True
        await _cleanup(db)
    _run(run())


def test_edge_needs_min_sample(db):
    async def run():
        await _cleanup(db)
        await _seed_regime_trades(db, "scalp", "ranging|low",
                                  [-30, -25, -40])  # only 3 — below min 8
        edge = await strategy_edge(db, UID, "ranging|low")
        assert edge["scalp"]["allowed"] is True
        await _cleanup(db)
    _run(run())


def test_regime_gate_end_to_end(db):
    async def run():
        await _cleanup(db)
        await db.intraday_candles.insert_one(
            {"user_id": UID, "symbol": "XAUUSD", "timeframe": "M15",
             "bars": _bars(), "updated_at": datetime.now(timezone.utc)})
        r = await detect(db, UID, force=True)
        await _seed_regime_trades(db, "scalp", r["key"],
                                  [-30, -25, -40, -22, -35, -28, -31, -26, -38])
        gate = await regime_gate(db, UID, {"regime_gating_enabled": True}, "scalp")
        assert gate["allowed"] is False
        assert gate["regime"]["key"] == r["key"]
        # toggle off → allowed
        gate2 = await regime_gate(db, UID, {"regime_gating_enabled": False}, "scalp")
        assert gate2["allowed"] is True
        # other strategy unaffected
        gate3 = await regime_gate(db, UID, {}, "trend")
        assert gate3["allowed"] is True
        await _cleanup(db)
    _run(run())
