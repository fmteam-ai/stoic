"""iter-86 — expanded multi-layer risk engine: monthly drawdown trip +
live volatility/liquidity/news/broker-anomaly layer evaluations."""
import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from circuit_breakers import (DEFAULT_MONTHLY_DRAWDOWN_PCT, _monthly_limit,
                              check_and_trip, month_ago_iso)
from risk_layers import evaluate_layers


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter86-{uuid.uuid4().hex[:8]}"


async def _seed_cfg(db, **over):
    cfg = {"user_id": UID, "account_id": None, "active": True,
           "risk_level": "medium", **over}
    res = await db.bot_configs.insert_one(cfg)
    cfg["_id"] = res.inserted_id
    return cfg


async def _cleanup(db):
    await db.bot_configs.delete_many({"user_id": UID})
    await db.trades.delete_many({"user_id": UID})
    await db.intraday_candles.delete_many({"user_id": UID})
    await db.accounts.delete_many({"user_id": UID})


# ---------------------------------------------------------------- monthly
def test_monthly_limit_defaults():
    assert _monthly_limit({"risk_level": "low"}) == DEFAULT_MONTHLY_DRAWDOWN_PCT["low"]
    assert _monthly_limit({"risk_level": "extreme"}) == 35.0
    assert _monthly_limit({"monthly_drawdown_pct": 5.5}) == 5.5
    assert _monthly_limit({}) == 12.0


def test_month_ago_iso_is_30_days():
    got = datetime.fromisoformat(month_ago_iso()).date()
    expect = (datetime.now(timezone.utc) - timedelta(days=30)).date()
    assert got == expect


def test_monthly_drawdown_trips(db):
    async def run():
        await _cleanup(db)
        cfg = await _seed_cfg(db)
        # loss 15 days ago (outside weekly window, inside monthly): -15% of 10k
        await db.trades.insert_one({
            "user_id": UID, "status": "closed", "origin": "auto",
            "pnl": -1500.0,
            "closed_at": (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()})
        res = await check_and_trip(db, UID, cfg,
                                   [{"equity": 10000.0, "balance": 10000.0}])
        assert res["tripped"] is True
        assert res["kind"] == "monthly"
        assert "Monthly drawdown" in res["reason"]
        stored = await db.bot_configs.find_one({"_id": cfg["_id"]})
        assert stored["active"] is False
        assert stored["tripped_kind"] == "monthly"
        await _cleanup(db)
    _run(run())


def test_monthly_within_limit_does_not_trip(db):
    async def run():
        await _cleanup(db)
        cfg = await _seed_cfg(db)
        await db.trades.insert_one({
            "user_id": UID, "status": "closed", "origin": "auto",
            "pnl": -500.0,  # -5% < 12% monthly limit
            "closed_at": (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()})
        res = await check_and_trip(db, UID, cfg,
                                   [{"equity": 10000.0, "balance": 10000.0}])
        assert res["tripped"] is False
        assert res["pnl_month"] == -500.0
        assert res["drawdown_month_pct"] == -5.0
        await _cleanup(db)
    _run(run())


def test_monthly_disabled_toggle(db):
    async def run():
        await _cleanup(db)
        cfg = await _seed_cfg(db, monthly_drawdown_enabled=False)
        await db.trades.insert_one({
            "user_id": UID, "status": "closed", "origin": "auto",
            "pnl": -3000.0,
            "closed_at": (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()})
        res = await check_and_trip(db, UID, cfg,
                                   [{"equity": 10000.0, "balance": 10000.0}])
        assert res["tripped"] is False
        assert res["monthly_enabled"] is False
        await _cleanup(db)
    _run(run())


def test_daily_takes_precedence_over_monthly(db):
    async def run():
        await _cleanup(db)
        cfg = await _seed_cfg(db)
        await db.trades.insert_one({
            "user_id": UID, "status": "closed", "origin": "auto",
            "pnl": -2000.0,
            "closed_at": datetime.now(timezone.utc).isoformat()})
        res = await check_and_trip(db, UID, cfg,
                                   [{"equity": 10000.0, "balance": 10000.0}])
        assert res["tripped"] is True
        assert res["kind"] == "daily"
        await _cleanup(db)
    _run(run())


# ------------------------------------------------------------- volatility
def _bars(n=96, rng=1.0, last_rng=None, fresh=True):
    now = time.time() if fresh else time.time() - 3600 * 3
    out = []
    for i in range(n):
        px = 4000.0
        r = rng if i < n - 1 else (last_rng if last_rng is not None else rng)
        out.append({"t": now - (n - 1 - i) * 900, "o": px, "h": px + r,
                    "l": px, "c": px + r / 2, "v": 100})
    return out


def test_volatility_layer_live_statuses(db):
    async def run():
        await _cleanup(db)
        # shock bar (>=4x median) → tripped
        await db.intraday_candles.insert_one({
            "user_id": UID, "symbol": "XAUUSD", "timeframe": "M15",
            "bars": _bars(last_rng=8.0), "updated_at": datetime.now(timezone.utc)})
        layers = await evaluate_layers(db, UID, None)
        vol = next(l for l in layers if l["layer"] == "volatility_stop")
        assert vol["status"] == "tripped", vol
        # normal bars → armed
        await db.intraday_candles.update_many(
            {"user_id": UID}, {"$set": {"bars": _bars()}})
        layers = await evaluate_layers(db, UID, None)
        vol = next(l for l in layers if l["layer"] == "volatility_stop")
        assert vol["status"] == "armed", vol
        # no candles at all → degraded (blind, never silently green)
        await db.intraday_candles.delete_many({"user_id": UID})
        layers = await evaluate_layers(db, UID, None)
        vol = next(l for l in layers if l["layer"] == "volatility_stop")
        assert vol["status"] == "degraded", vol
        await _cleanup(db)
    _run(run())


# ------------------------------------------------- broker anomaly / other
def test_broker_anomaly_counts_blocked_accounts(db):
    async def run():
        await _cleanup(db)
        await db.accounts.insert_one({
            "user_id": UID, "status": "active", "trading_blocked": True,
            "bridge_token": f"iter86-{uuid.uuid4().hex}",
            "block_reason": "reject streak", "label": "T"})
        layers = await evaluate_layers(db, UID, None)
        ba = next(l for l in layers if l["layer"] == "broker_anomaly_protection")
        assert ba["status"] == "tripped"
        await db.accounts.update_many({"user_id": UID},
                                      {"$set": {"trading_blocked": False}})
        layers = await evaluate_layers(db, UID, None)
        ba = next(l for l in layers if l["layer"] == "broker_anomaly_protection")
        assert ba["status"] == "armed"
        await _cleanup(db)
    _run(run())


def test_all_twelve_layers_present_and_isolated(db):
    async def run():
        await _cleanup(db)
        layers = await evaluate_layers(db, UID, None)
        assert len(layers) == 12
        keys = {l["layer"] for l in layers}
        assert {"volatility_stop", "liquidity_protection", "news_protection",
                "broker_anomaly_protection", "monthly_drawdown_stop",
                "weekly_loss_stop", "circuit_breaker"} <= keys
        for l in layers:
            assert l["status"] in ("armed", "degraded", "tripped", "error")
            assert l["detail"]
        await _cleanup(db)
    _run(run())


def test_liquidity_layer_reports_gate_mode(db):
    async def run():
        await _cleanup(db)
        await _seed_cfg(db, liquidity_gate_mode="off")
        layers = await evaluate_layers(db, UID, None)
        liq = next(l for l in layers if l["layer"] == "liquidity_protection")
        assert liq["status"] == "degraded"
        assert "OFF" in liq["detail"]
        await db.bot_configs.update_many(
            {"user_id": UID}, {"$set": {"liquidity_gate_mode": "enforce"}})
        layers = await evaluate_layers(db, UID, None)
        liq = next(l for l in layers if l["layer"] == "liquidity_protection")
        assert liq["status"] == "armed"
        assert "DOM" in liq["detail"]
        await _cleanup(db)
    _run(run())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
