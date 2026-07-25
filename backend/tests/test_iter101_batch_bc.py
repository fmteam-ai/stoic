"""iter-101 — Batch B/C tiers: T11 AI Coach, T12 Replay steps,
T14 Chaos drills, T5 Marketplace."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from chaos_drills import run_drills
from coach import STAGE_EXPLAIN, coach_cards
from marketplace import strategy_cards


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter101-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------- AI coach
def test_coach_cards_compose(db):
    async def go():
        now = datetime.now(timezone.utc).isoformat()
        await db.bot_configs.insert_one({
            "user_id": UID, "account_id": "coach-acc", "active": True,
            "_last_notable_pulse": {"ts": now, "symbol": "XAUUSD",
                                    "reason": "consensus 48 < 55",
                                    "action": "HOLD"}})
        await db.trade_decisions.insert_many([
            {"user_id": UID, "status": "rejected", "stage": "news_gate",
             "symbol": "XAUUSD", "reason": "news gate veto", "ts": now},
            {"user_id": UID, "status": "rejected", "stage": "news_gate",
             "symbol": "XAUUSD", "reason": "news gate veto", "ts": now},
            {"user_id": UID, "status": "rejected", "stage": "anti_pyramid",
             "symbol": "BTCUSD", "reason": "anti-pyramid", "ts": now},
        ])
        await db.signals.insert_one({
            "user_id": UID, "symbol": "XAUUSD", "created_at": now,
            "adaptive_sizing": {"multiplier": 0.4, "risk_pct": 0.2,
                                "drawdown_frac": 0.08,
                                "recent_win_rate": 0.4,
                                "components": {"drawdown": 0.6,
                                               "confidence": 0.8,
                                               "volatility": 1.1}}})
        await db.trade_evaluations.insert_one({
            "user_id": UID, "symbol": "XAUUSD", "outcome": "loss",
            "lesson": "sold into compression primed for expansion",
            "mistakes": ["counter_trend_entry"]})
        out = await coach_cards(db, UID)
        assert out["waiting"][0]["reason"].startswith("consensus")
        stages = {r["stage"]: r for r in out["rejected"]}
        assert stages["news_gate"]["count"] == 2
        assert stages["news_gate"]["coach"] == STAGE_EXPLAIN["news_gate"]
        assert out["risk"]["risk_pct"] == 0.2
        assert any("drawdown" in d for d in out["risk"]["drivers"])
        assert "expansion" in out["lesson"]["lesson"]
        for c in ("bot_configs", "trade_decisions", "signals",
                  "trade_evaluations"):
            await getattr(db, c).delete_many({"user_id": UID})
    _run(go())


def test_coach_empty_user(db):
    async def go():
        out = await coach_cards(db, f"nobody-{UID}")
        assert out["waiting"] == [] and out["rejected"] == []
        assert out["risk"] is None and out["lesson"] is None
    _run(go())


# ------------------------------------------------------------ replay steps
def test_replay_steps_include_decision(db):
    async def go():
        from operator_tools import trade_replay
        uid = f"{UID}-rp"
        now = datetime.now(timezone.utc)
        sid = (await db.signals.insert_one({
            "user_id": uid, "confidence": 72,
            "consensus": {"score": 61},
            "monte_carlo": {"ev_r_net": 0.4}})).inserted_id
        tid = (await db.trades.insert_one({
            "user_id": uid, "symbol": "XAUUSD", "action": "BUY",
            "status": "closed", "signal_id": str(sid),
            "entry_price": 4000.0, "exit_price": 4010.0, "pnl": 25.0,
            "stop_loss": 3995.0, "close_reason": "take_profit",
            "opened_at": now.isoformat(),
            "closed_at": now.isoformat()})).inserted_id
        trade = await db.trades.find_one({"_id": tid})
        out = await trade_replay(db, trade)
        kinds = [s["kind"] for s in out["steps"]]
        assert kinds[0] == "DECISION"
        assert "confidence 72%" in out["steps"][0]["label"]
        assert "ENTRY" in kinds and "CLOSED" in kinds
        await db.signals.delete_many({"user_id": uid})
        await db.trades.delete_many({"user_id": uid})
    _run(go())


# ------------------------------------------------------------ chaos drills
def test_chaos_drills_all_pass_and_persist(db):
    async def go():
        out = await run_drills(db)
        assert out["total"] == 5
        by = {r["drill"]: r for r in out["results"]}
        assert by["duplicate_order"]["passed"] is True
        assert by["volatility_shock"]["passed"] is True
        assert by["worker_crash"]["passed"] is True
        assert by["db_recovery"]["passed"] is True
        assert by["broker_disconnect"]["passed"] is True
        doc = await db.chaos_drills.find_one({}, sort=[("at", -1)])
        assert doc and doc["total"] == 5
        # synthetic residue cleaned up
        assert await db.broker_deals.count_documents(
            {"account_id": "chaos-drill"}) == 0
        assert await db.worker_leases.count_documents({"chaos": True}) == 0
        assert await db.chaos_probe.count_documents({}) == 0
    _run(go())


# ------------------------------------------------------------- marketplace
def test_marketplace_cards_with_verified_perf(db):
    async def go():
        uid = f"{UID}-mk"
        now = datetime.now(timezone.utc).isoformat()
        await db.trades.insert_many([
            {"user_id": uid, "status": "closed", "origin": "auto",
             "scope": "hf_scalp", "pnl": p, "closed_at": now}
            for p in (30.0, -10.0, 25.0, 15.0)])
        await db.bot_configs.insert_one({
            "user_id": uid, "account_id": "mk-acc", "active": True,
            "active_preset": "scalper"})
        out = await strategy_cards(db, uid)
        cards = {c["key"]: c for c in out["strategies"]}
        sc = cards["scalper"]
        assert sc["verified"] is True
        assert sc["performance"]["trades"] == 4
        assert sc["performance"]["pnl"] == 60.0
        assert sc["performance"]["profit_factor"] == 7.0
        assert sc["performance"]["max_drawdown_usd"] == 10.0
        assert sc["stars"] == 5
        assert sc["risk_class"] == "ACTIVE"
        assert cards["sniper"]["verified"] is False
        assert cards["sniper"]["stars"] is None
        await db.trades.delete_many({"user_id": uid})
        await db.bot_configs.delete_many({"user_id": uid})
    _run(go())


def test_marketplace_empty_user(db):
    async def go():
        out = await strategy_cards(db, f"nobody-{UID}")
        assert len(out["strategies"]) >= 6
        assert all(c["performance"] is None for c in out["strategies"])
    _run(go())
