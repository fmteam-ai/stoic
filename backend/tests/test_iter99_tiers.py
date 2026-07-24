"""iter-99 — Tiers 1/3/7/9/16: decision DNA, market state, auto-defensive,
execution forecast, broker style suitability."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from broker_intel import style_suitability
from decision_dna import compose_dna
from differentiation import verify_attestation


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter99-{uuid.uuid4().hex[:8]}"


# ----------------------------------------------------------- decision DNA
def test_compose_dna_signed_and_complete(db):
    async def go():
        now = datetime.now(timezone.utc).isoformat()
        sid = (await db.signals.insert_one({
            "user_id": UID, "symbol": "XAUUSD", "action": "BUY",
            "confidence": 74, "reasoning": "trend pullback at H1 demand",
            "monte_carlo": {"ev_r_net": 0.31},
            "uncertainty": {"confidence_pct": 68, "risk": "MEDIUM"},
            "consensus": {"score": 66}, "spread": 2.2,
            "news_ai": {"net": 0.4}, "strategy_engine": "mtf_v2",
            "trend_score": {"quality": {"score": 78, "direction": "UP",
                                        "components": {"volatility_support": 62}}},
            "created_at": now})).inserted_id
        tid = (await db.trades.insert_one({
            "user_id": UID, "account_id": f"acc-{UID}", "symbol": "XAUUSD",
            "action": "BUY", "status": "closed", "origin": "auto",
            "pnl": 55.0, "signal_id": str(sid), "scope": "mtf",
            "risk_pct": 0.5, "lot_size": 0.2, "entry_price": 4000.0,
            "stop_loss": 3995.0, "take_profit": 4012.0,
            "close_reason": "take_profit", "market_regime": {"key": "trending_up|normal"},
            "versions": {"strategy_version": "mtf_v2"},
            "opened_at": now, "closed_at": now})).inserted_id
        await db.trade_evaluations.insert_one(
            {"trade_id": str(tid), "user_id": UID,
             "mfe_r": 2.5, "mae_r": -0.3, "realized_r": 2.4})
        trade = await db.trades.find_one({"_id": tid})
        dna = await compose_dna(db, trade)
        assert dna["confidence"]["raw"] == 74
        assert dna["expected_value_r"] == 0.31
        assert dna["trend_strength"]["score"] == 78
        assert dna["liquidity_score"] is not None
        assert dna["four_questions"]["outcome_vs_expectation"]["verdict"] == \
            "beat expectation"
        assert "why" in str(dna["four_questions"]["why_decided"]).lower() or \
            "BUY" in dna["four_questions"]["why_decided"]
        att = dna["attestation"]
        assert verify_attestation(att["payload_hash"], att["signature"])
        for c in ("signals", "trades", "trade_evaluations"):
            await getattr(db, c).delete_many({"user_id": UID})
    _run(go())


# ----------------------------------------------------------- market state
def test_market_state_score_shape(db):
    async def go():
        from market_state import market_state_score
        out = await market_state_score(db, f"nobody-{UID}")
        assert set(out["scores"]) == {"trend", "volatility", "vol_stability",
                                      "liquidity", "news_risk", "confidence"}
        assert 0 <= out["market_health"] <= 100
        for v in out["scores"].values():
            assert 0 <= v <= 100
    _run(go())


# ------------------------------------------------------- style suitability
def test_style_suitability_blends():
    comps = {"fill_speed": {"score": 90}, "spread": {"score": 80},
             "slippage": {"score": 70}, "rejects": {"score": 95},
             "freeze": {"score": 88}}
    s = style_suitability(comps)
    assert set(s) == {"scalping", "swing", "gold", "indices", "crypto"}
    assert all(0 <= v <= 100 for v in s.values() if v is not None)
    # fast fills suit scalping more than slow ones
    slow = style_suitability({**comps, "fill_speed": {"score": 20}})
    assert slow["scalping"] < s["scalping"]
    # missing everything → None (no fabricated confidence)
    assert style_suitability({})["scalping"] is None


# --------------------------------------------------------- auto-defensive
def test_portfolio_trip_demotes_to_defensive(db):
    async def go():
        uid = f"{UID}-def"
        from risk_layers import _trip_configs
        await db.bot_configs.insert_many([
            {"user_id": uid, "account_id": "d1", "active": True,
             "operational_mode": "autonomous_live"},
            {"user_id": uid, "account_id": "d2", "active": True,
             "operational_mode": "observe"},
        ])
        cfgs = [c async for c in db.bot_configs.find({"user_id": uid})]
        await _trip_configs(db, cfgs, "test breach")
        by = {c["account_id"]: c async for c in
              db.bot_configs.find({"user_id": uid})}
        assert by["d1"]["operational_mode"] == "defensive"
        assert not by["d1"]["active"]
        assert by["d2"]["operational_mode"] == "observe"  # not live → untouched
        led = await db.governed_changes.find_one(
            {"user_id": uid, "source": "risk_commander"})
        assert led and led["new_value"] == "defensive"
        await db.bot_configs.delete_many({"user_id": uid})
        await db.governed_changes.delete_many({"user_id": uid})
    _run(go())
