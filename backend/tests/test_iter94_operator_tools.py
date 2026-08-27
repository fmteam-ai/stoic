"""iter-94 — Phase 7 operator tools: replay, decision timeline, what-if."""
import asyncio
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

from operator_tools import (decision_timeline, simulate_exit, trade_replay,
                            what_if, _equity_stats)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter94-{uuid.uuid4().hex[:8]}"


async def _cleanup(db):
    for c in ("trades", "price_ticks", "intraday_candles", "signals",
              "trade_events", "broker_deals", "journal_cards"):
        await getattr(db, c).delete_many({"user_id": UID})
    await db.price_ticks.delete_many({"symbol": "IT94USD"})


# ------------------------------------------------------------- simulator
def test_simulate_exit_paths():
    bars_up = [{"h": 4000 + i, "l": 3998 + i, "c": 3999 + i} for i in range(30)]
    r = simulate_exit("BUY", 4000, 3990, 4020, bars_up)
    assert r["reason"] == "tp" and abs(r["r"] - 2.0) < 1e-9
    bars_dn = [{"h": 4001 - i, "l": 3999 - i, "c": 4000 - i} for i in range(30)]
    r = simulate_exit("BUY", 4000, 3990, 4050, bars_dn)
    assert r["reason"] == "sl" and abs(r["r"] + 1.0) < 1e-9
    # trailing locks profit: up 15 then reverse hard
    bars = ([{"h": 4000 + i, "l": 3999 + i, "c": 4000 + i} for i in range(15)]
            + [{"h": 4015 - i * 3, "l": 4012 - i * 3, "c": 4013 - i * 3} for i in range(10)])
    no_trail = simulate_exit("BUY", 4000, 3990, 4100, bars)
    trail = simulate_exit("BUY", 4000, 3990, 4100, bars, trailing_start_r=1.0)
    assert trail["r"] > no_trail["r"]          # trailing kept profit
    assert trail["reason"] == "sl" and trail["r"] > 0
    # SELL mirror
    r = simulate_exit("SELL", 4000, 4010, 3980,
                      [{"h": 4001 - i, "l": 3998 - i, "c": 4000 - i} for i in range(30)])
    assert r["reason"] == "tp" and abs(r["r"] - 2.0) < 1e-9


def test_equity_stats():
    s = _equity_stats([100, -50, -60, 200])
    assert s["total_pnl"] == 190 and s["max_drawdown"] == 110
    assert s["win_rate"] == 0.5 and s["n"] == 4


# ---------------------------------------------------------------- replay
def test_trade_replay_ticks_and_fallback(db):
    async def run():
        await _cleanup(db)
        t0 = time.time() - 3600
        for i in range(120):
            await db.price_ticks.insert_one({
                "symbol": "IT94USD", "ts": t0 + i * 20,
                "price": 4000 + i * 0.1, "bid": 4000 + i * 0.1,
                "ask": 4000.3 + i * 0.1, "user_id": UID})
        trade = {"_id": "x1", "user_id": UID, "symbol": "IT94USD",
                 "action": "BUY", "entry_price": 4001.0, "stop_loss": 3995.0,
                 "tp1": 4010.0, "exit_price": 4008.0, "pnl": 70.0,
                 "opened_at": datetime.fromtimestamp(t0 + 60, tz=timezone.utc).isoformat(),
                 "closed_at": datetime.fromtimestamp(t0 + 2000, tz=timezone.utc).isoformat()}
        rep = await trade_replay(db, trade)
        assert rep["source"] == "ticks"
        assert len(rep["ticks"]) > 50
        kinds = [m["kind"] for m in rep["markers"]]
        assert "ENTRY" in kinds and "EXIT" in kinds
        assert rep["levels"]["stop_loss"] == 3995.0
        # no ticks → M15 fallback path (empty here → note)
        trade2 = {**trade, "symbol": "NOPEUSD", "_id": "x2"}
        rep2 = await trade_replay(db, trade2)
        assert rep2["source"] == "m15_bars"
        await _cleanup(db)
    _run(run())


# ---------------------------------------------------------- timeline
def test_decision_timeline_stages(db):
    async def run():
        await _cleanup(db)
        sid = (await db.signals.insert_one(
            {"user_id": UID, "confidence": 70, "engine_label": "Trend",
             "risk_engine": {"ok": True},
             "created_at": datetime.now(timezone.utc).isoformat()})).inserted_id
        res = await db.trades.insert_one({
            "user_id": UID, "symbol": "XAUUSD", "action": "BUY",
            "status": "closed", "signal_id": str(sid), "lot_size": 0.1,
            "sl_pips": 30, "mt5_ticket": 12345, "position_id": 12345,
            "account_id": "acc94", "entry_price": 4000.0, "pnl": 55.0,
            "_dispatched_at": "2026-07-24T08:00:00+00:00",
            "acknowledged_at": "2026-07-24T08:00:01+00:00",
            "breakeven_set": True,
            "closed_at": "2026-07-24T10:00:00+00:00"})
        await db.broker_deals.insert_one({
            "user_id": UID, "account_id": "acc94", "mt5_ticket": 12345,
            "deal_id": 999901, "deal_entry": "in", "deal_time": time.time()})
        trade = await db.trades.find_one({"_id": res.inserted_id})
        tl = await decision_timeline(db, trade)
        stages = {s["stage"]: s["status"] for s in tl["stages"]}
        assert [s["stage"] for s in tl["stages"]] == [
            "signal", "risk", "order_check", "broker", "deal",
            "protection", "reconciliation", "journal"]
        for st in ("signal", "risk", "order_check", "broker", "deal",
                   "protection", "reconciliation"):
            assert stages[st] == "complete", st
        assert stages["journal"] == "pending"
        await _cleanup(db)
    _run(run())


# ---------------------------------------------------------------- what-if
def test_what_if_risk_rescale(db):
    async def run():
        await _cleanup(db)
        now = datetime.now(timezone.utc)
        for p in (100.0, -50.0, 80.0):
            await db.trades.insert_one({
                "user_id": UID, "origin": "auto", "status": "closed",
                "pnl": p, "symbol": "XAUUSD", "action": "BUY",
                "entry_price": 4000.0, "stop_loss": 3990.0, "risk_pct": 1.0,
                "opened_at": (now - timedelta(days=2)).isoformat(),
                "closed_at": (now - timedelta(days=1)).isoformat()})
        res = await what_if(db, UID, risk_pct=0.5)
        assert res["mode"] == "risk_rescale"
        assert res["baseline"]["total_pnl"] == 130.0
        assert res["simulated"]["total_pnl"] == 65.0
        assert res["delta"]["total_pnl"] == -65.0
        assert res["coverage"]["simulated"] == 3
        await _cleanup(db)
    _run(run())


def test_what_if_geometry_replay(db):
    async def run():
        await _cleanup(db)
        now = datetime.now(timezone.utc)
        t0 = time.time() - 86400
        bars = [{"t": t0 + i * 900, "o": 4000 + i * 0.5, "h": 4001 + i * 0.5,
                 "l": 3999 + i * 0.5, "c": 4000 + i * 0.5, "v": 1}
                for i in range(96)]
        await db.intraday_candles.insert_one(
            {"user_id": UID, "symbol": "XAUUSD", "timeframe": "M15",
             "bars": bars, "updated_at": now})
        await db.trades.insert_one({
            "user_id": UID, "origin": "auto", "status": "closed",
            "pnl": 100.0, "symbol": "XAUUSD", "action": "BUY",
            "entry_price": 4000.0, "stop_loss": 3990.0, "tp1": 4010.0,
            "risk_pct": 1.0,
            "opened_at": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(),
            "closed_at": (now - timedelta(hours=1)).isoformat()})
        res = await what_if(db, UID, tp_mult=2.0)
        assert res["mode"] == "bar_replay"
        assert res["coverage"]["simulated"] == 1
        # wider TP on a steady uptrend → simulated ≥ baseline
        assert res["simulated"]["total_pnl"] >= res["baseline"]["total_pnl"]
        await _cleanup(db)
    _run(run())


def test_what_if_no_trades(db):
    async def run():
        res = await what_if(db, f"ghost-{uuid.uuid4().hex[:6]}", risk_pct=0.5)
        assert res["error"]
    _run(run())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
