"""iter-100 — Batch A tiers: T6 Digital Twin, T13 Strategy Genetics,
T17 Calibration card summary."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from digital_twin import twin_summary
from strategy_genetics import lineage


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter100-{uuid.uuid4().hex[:8]}"
SYM = f"TWINSYM{uuid.uuid4().hex[:6].upper()}"


# ------------------------------------------------------------ digital twin
def test_twin_replays_intercepts_and_compares(db):
    async def go():
        now = datetime.now(timezone.utc)
        epoch = now.timestamp()
        # bars rising straight to TP for a BUY intercepted 1h ago
        bars = [{"t": epoch - 3600 + i * 900, "o": 100 + i, "h": 101 + i,
                 "l": 99.6 + i, "c": 100.5 + i} for i in range(20)]
        await db.intraday_candles.insert_one(
            {"symbol": SYM, "timeframe": "M15", "bars": bars,
             "user_id": UID})
        await db.trade_decisions.insert_one({
            "user_id": UID, "account_id": "twin-acc-1", "symbol": SYM,
            "status": "rejected", "stage": "news_gate", "reason": "test",
            "ts": (now - timedelta(minutes=55)).isoformat(),
            "signal": {"action": "BUY", "entry_price": 101.0,
                       "stop_loss": 99.0, "take_profit": 105.0}})
        await db.trades.insert_one({
            "user_id": UID, "account_id": "twin-acc-1", "symbol": SYM,
            "status": "closed", "origin": "auto", "pnl": 40.0,
            "risk_amount": 50.0,
            "closed_at": now.isoformat()})
        out = await twin_summary(db, UID, days=7)
        assert out["totals"]["intercepts_replayed"] == 1
        acc = next(a for a in out["accounts"]
                   if a["account_id"] == "twin-acc-1")
        assert acc["twin"]["would_win"] == 1
        assert acc["twin"]["alt_r"] == 2.0          # (105-101)/(101-99)
        assert acc["twin"]["alt_pnl_est"] == 100.0  # 2R × $50 risk
        assert acc["twin"]["twin_pnl_est"] == 140.0
        assert acc["verdict"] == "GATES COSTING EDGE"
        assert acc["live"]["pnl"] == 40.0
        assert out["top_divergences"][0]["stage"] == "news_gate"
        for c in ("intraday_candles", "trade_decisions", "trades"):
            await getattr(db, c).delete_many({"user_id": UID})
    _run(go())


def test_twin_gates_protecting_on_sl_first(db):
    async def go():
        uid = f"{UID}-sl"
        now = datetime.now(timezone.utc)
        epoch = now.timestamp()
        # bars falling straight through the SL for a BUY
        bars = [{"t": epoch - 3600 + i * 900, "o": 100 - i, "h": 100.4 - i,
                 "l": 98.5 - i, "c": 99 - i} for i in range(20)]
        sym = f"{SYM}B"
        await db.intraday_candles.insert_one(
            {"symbol": sym, "timeframe": "M15", "bars": bars,
             "user_id": uid})
        await db.trade_decisions.insert_one({
            "user_id": uid, "account_id": "twin-acc-2", "symbol": sym,
            "status": "rejected", "stage": "risk_engine", "reason": "test",
            "ts": (now - timedelta(minutes=55)).isoformat(),
            "signal": {"action": "BUY", "entry_price": 100.0,
                       "stop_loss": 99.0, "take_profit": 108.0}})
        out = await twin_summary(db, uid, days=7)
        acc = out["accounts"][0]
        assert acc["twin"]["would_lose"] == 1
        assert acc["twin"]["alt_r"] == -1.0
        assert acc["verdict"] == "GATES PROTECTING"
        await db.intraday_candles.delete_many({"user_id": uid})
        await db.trade_decisions.delete_many({"user_id": uid})
    _run(go())


def test_twin_empty_user(db):
    async def go():
        out = await twin_summary(db, f"nobody-{UID}", days=7)
        assert out["accounts"] == []
        assert out["totals"]["intercepts_replayed"] == 0
    _run(go())


# ------------------------------------------------------- strategy genetics
def test_genetics_lineage_composes_all_sources(db):
    async def go():
        uid = f"{UID}-gen"
        now = datetime.now(timezone.utc).isoformat()
        await db.governed_changes.insert_one({
            "user_id": uid, "field": "risk_pct", "old_value": 0.5,
            "new_value": 0.8, "classification": "aggressive",
            "status": "rejected", "source": "manual_proposal",
            "proposed_at": now, "detail": "test raise"})
        await db.auto_guards.insert_one({
            "user_id": uid, "active": True, "created_at": now,
            "measure": {"title": "Pause off-session trades",
                        "rationale": "0% WR off session"},
            "evidence": {"net": 212.0}})
        await db.trades.insert_many([
            {"user_id": uid, "status": "closed", "origin": "auto",
             "pnl": 30.0, "versions": {"strategy_version": "hf_scalp_v132"}},
            {"user_id": uid, "status": "closed", "origin": "auto",
             "pnl": -10.0, "versions": {"strategy_version": "hf_scalp_v132"}},
        ])
        out = await lineage(db, uid)
        kinds = {e["kind"] for e in out["events"]}
        assert {"governance", "auto_guard"} <= kinds
        gov = next(e for e in out["events"] if e["kind"] == "governance")
        assert "risk_pct" in gov["what"] and gov["status"] == "rejected"
        guard = next(e for e in out["events"] if e["kind"] == "auto_guard")
        assert guard["helped"] == 212.0 and guard["status"] == "active"
        hf = next(v for v in out["versions"] if v["engine"] == "hf_scalp")
        assert hf["performance"]["n"] == 2
        assert hf["performance"]["pnl"] == 20.0
        assert hf["performance"]["win_rate"] == 50.0
        assert out["policies"]["risk_policy"].startswith("risk_")
        for c in ("governed_changes", "auto_guards", "trades"):
            await getattr(db, c).delete_many({"user_id": uid})
    _run(go())


def test_genetics_empty_user(db):
    async def go():
        out = await lineage(db, f"nobody-{UID}")
        assert out["events"] == []
        assert all(v["performance"] is None for v in out["versions"])
    _run(go())


# ------------------------------------------------------------- calibration
def test_calibration_summary_math(db):
    async def go():
        from calibration import compute_calibration
        uid = f"{UID}-cal"
        now = datetime.now(timezone.utc).isoformat()
        docs = []
        for i in range(6):
            docs.append({"user_id": uid, "status": "closed",
                         "origin": "auto", "pnl": 20.0 if i < 4 else -20.0,
                         "confidence": 70, "scope": "hf_scalp",
                         "risk_amount": 20.0, "closed_at": now})
        await db.trades.insert_many(docs)
        table = await compute_calibration(db, uid, days=30)
        ent = table["hf_scalp"]
        assert ent["n"] == 6 and ent["wins"] == 4
        b = ent["buckets"][0]
        assert b["stated"] == 70.0
        assert b["realized"] == round(4 / 6 * 100, 1)
        assert b["gap"] == round(4 / 6 * 100 - 70, 1)
        # endpoint summary math (weighted) mirrors the route implementation
        err = abs(b["gap"])
        assert err == round(abs(66.7 - 70.0), 1)
        await db.trades.delete_many({"user_id": uid})
    _run(go())
