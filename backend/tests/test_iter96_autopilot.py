"""iter-96 — Autopilot: probabilistic regime, failure classifier,
learning records, change governance."""
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

from adaptive_sizing import regime_certainty_mult
from change_governance import classify_change, propose_change, resolve_change
from failure_classifier import CATEGORIES, classify_failure
from learning_record import build_learning_record, failure_summary
from market_regime import REGIME_CLASSES, regime_probabilities


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter96-{uuid.uuid4().hex[:8]}"


# ------------------------------------------------- probabilistic regime
def _bars(n=40, drift=0.0, rng=1.0, last_range=None):
    out = []
    px = 4000.0
    for i in range(n):
        px += drift
        r = rng if (last_range is None or i < n - 1) else last_range
        out.append({"o": px, "h": px + r / 2, "l": px - r / 2, "c": px,
                    "t": i * 900})
    return out


def test_regime_probabilities_sum_and_classes():
    p = regime_probabilities(_bars(), "trending_up", 0.8,
                             {"ratio": 1.0}, False)
    assert set(p["classes"]) == set(REGIME_CLASSES)
    assert abs(sum(p["classes"].values()) - 1.0) < 0.02
    assert p["top"] == "strong_trend"
    assert 0.0 <= p["uncertainty"] <= 1.0


def test_regime_probabilities_signals():
    # volatility expansion dominates on high ratio + ranging
    p = regime_probabilities(_bars(), "ranging", 0.2, {"ratio": 1.9}, False)
    assert p["classes"]["volatility_expansion"] > p["classes"]["strong_trend"]
    # abnormal bar detected
    p = regime_probabilities(_bars(last_range=6.0), "ranging", 0.2,
                             {"ratio": 1.0}, False)
    assert p["classes"]["abnormal"] > 0.15
    # news flag lifts news_driven
    p_news = regime_probabilities(_bars(), "ranging", 0.2, {"ratio": 1.0}, True)
    p_no = regime_probabilities(_bars(), "ranging", 0.2, {"ratio": 1.0}, False)
    assert p_news["classes"]["news_driven"] > p_no["classes"]["news_driven"]
    # confident trend = lower uncertainty than mixed evidence
    certain = regime_probabilities(_bars(), "trending_up", 0.95,
                                   {"ratio": 1.0}, False)
    mixed = regime_probabilities(_bars(last_range=6.0), "trending_up", 0.55,
                                 {"ratio": 1.5}, True)
    assert certain["uncertainty"] < mixed["uncertainty"]


def test_regime_certainty_mult_tiers():
    assert regime_certainty_mult(None) == 1.0
    assert regime_certainty_mult(0.4) == 1.0
    assert regime_certainty_mult(0.65) == 0.9
    assert regime_certainty_mult(0.8) == 0.8
    assert regime_certainty_mult(0.95) == 0.65


# -------------------------------------------------- failure classifier
BASE_TRADE = {"symbol": "XAUUSD", "action": "BUY", "pnl": -50.0,
              "user_id": UID, "risk_pct": 0.5}


def test_failure_priority_order():
    # data beats everything
    v = classify_failure(BASE_TRADE, {"pipeline_safe_to_execute": False},
                         {"mfe_r": 0.0}, ["OrderRejected"], {})
    assert v["category"] == "data_failure"
    # operational beats execution
    v = classify_failure({**BASE_TRADE, "pnl_estimated": True,
                          "slippage_pips": 9.0})
    assert v["category"] == "operational_failure"
    # execution: big slippage
    v = classify_failure({**BASE_TRADE, "slippage_pips": 5.0})
    assert v["category"] == "execution_failure"
    # risk: oversized
    v = classify_failure({**BASE_TRADE, "risk_pct": 3.0})
    assert v["category"] == "risk_failure"
    # risk: correlated exposure
    v = classify_failure(BASE_TRADE, context={"correlated_open": 4})
    assert v["category"] == "risk_failure"
    # regime: self-eval mistake
    v = classify_failure(BASE_TRADE,
                         evaluation={"mistakes": ["counter_trend_entry"],
                                     "mfe_r": 0.5})
    assert v["category"] == "regime_failure"
    # signal: never worked
    v = classify_failure(BASE_TRADE, evaluation={"mfe_r": 0.05})
    assert v["category"] == "signal_failure"
    # normal: clean loss with decent MFE
    v = classify_failure(BASE_TRADE, evaluation={"mfe_r": 0.6})
    assert v["category"] == "normal_statistical_loss"
    assert v["route_fix_to"].startswith("no strategy change")
    assert all(v["category"] in CATEGORIES for v in [v])


def test_failure_small_slippage_is_not_execution():
    v = classify_failure({**BASE_TRADE, "slippage_pips": 0.8},
                         evaluation={"mfe_r": 0.5})
    assert v["category"] == "normal_statistical_loss"


# ---------------------------------------------------- learning record
def test_build_learning_record(db):
    async def go():
        now = datetime.now(timezone.utc).isoformat()
        sig = {"user_id": UID, "symbol": "XAUUSD", "action": "SELL",
               "confidence": 72,
               "monte_carlo": {"ev_r": 0.2, "ev_r_net": 0.12},
               "uncertainty": {"confidence_pct": 64, "risk": "MEDIUM"},
               "consensus": {"score": 61},
               "news_ai": {"net": -1.2}, "created_at": now}
        sid = (await db.signals.insert_one(sig)).inserted_id
        tid = (await db.trades.insert_one({
            "user_id": UID, "account_id": f"acc-{UID}", "symbol": "XAUUSD",
            "action": "SELL", "status": "closed", "origin": "auto",
            "pnl": -40.0, "signal_id": str(sid), "scope": "hf_scalp",
            "strategy_class": "scalp", "slippage_pips": 0.5,
            "risk_pct": 0.4, "lot_size": 0.1, "close_reason": "stop_loss",
            "opened_at": now, "closed_at": now,
            "versions": {"strategy_version": "hf_v1"}})).inserted_id
        await db.trade_evaluations.insert_one(
            {"trade_id": str(tid), "user_id": UID, "mfe_r": 0.4,
             "mae_r": -1.0, "realized_r": -1.0})
        trade = await db.trades.find_one({"_id": tid})
        rec = await build_learning_record(db, trade)
        assert rec["entry_confidence"] == 72
        assert rec["expected_value_r"] == 0.12
        assert rec["mfe_r"] == 0.4 and rec["realized_r"] == -1.0
        assert rec["strategy"]["scope"] == "hf_scalp"
        assert rec["model_versions"]["strategy_version"] == "hf_v1"
        assert rec["model_disagreement"]["consensus_score"] == 61
        assert rec["external_events"]["news_net"] == -1.2
        assert rec["failure"]["category"] in CATEGORIES
        # summary aggregates it
        await db.learning_records.insert_one(rec)
        summ = await failure_summary(db, UID, days=7)
        assert summ["classified_losses"] >= 1
        assert summ["categories"][0]["route_fix_to"]
        for c in ("signals", "trades", "trade_evaluations",
                  "learning_records"):
            await getattr(db, c).delete_many({"user_id": UID})
    _run(go())


# ------------------------------------------------------- governance
def test_classify_change_policy():
    assert classify_change("risk_pct", 1.0, 0.5) == "conservative"
    assert classify_change("risk_pct", 0.5, 1.0) == "aggressive"
    assert classify_change("min_confidence_override", 55, 65) == "conservative"
    assert classify_change("min_confidence_override", 65, 55) == "aggressive"
    assert classify_change("loss_cooldown_minutes", 30, 60) == "conservative"
    assert classify_change("kelly_enabled", False, True) == "aggressive"
    assert classify_change("kelly_enabled", True, False) == "conservative"
    assert classify_change("daily_drawdown_pct", 3.0, 6.0) == "aggressive"
    assert classify_change("bridge_token", "a", "b") == "forbidden"
    assert classify_change("some_unknown_knob", 1, 2) == "aggressive"
    assert classify_change("risk_pct", 0.5, 0.5) == "conservative"


def test_governance_propose_and_resolve(db):
    async def go():
        uid = f"{UID}-gov"
        await db.bot_configs.insert_one(
            {"user_id": uid, "account_id": f"acc-{uid}", "active": True,
             "risk_pct": 1.0, "min_confidence_override": 55})
        # conservative → auto-applied
        d1 = await propose_change(db, uid, "risk_pct", 1.0, 0.5,
                                  source="test")
        assert d1["status"] == "auto_applied" and d1["updated_configs"] == 1
        cfg = await db.bot_configs.find_one({"user_id": uid})
        assert cfg["risk_pct"] == 0.5
        # aggressive → pending, config untouched
        d2 = await propose_change(db, uid, "risk_pct", 0.5, 1.5,
                                  source="test")
        assert d2["status"] == "pending"
        cfg = await db.bot_configs.find_one({"user_id": uid})
        assert cfg["risk_pct"] == 0.5
        # approve applies it
        r = await resolve_change(db, uid, d2["_id"], approve=True)
        assert r["ok"] and r["status"] == "approved"
        cfg = await db.bot_configs.find_one({"user_id": uid})
        assert cfg["risk_pct"] == 1.5
        # double-resolve refused
        r2 = await resolve_change(db, uid, d2["_id"], approve=True)
        assert not r2["ok"]
        # reject path
        d3 = await propose_change(db, uid, "kelly_enabled", False, True,
                                  source="test")
        r3 = await resolve_change(db, uid, d3["_id"], approve=False)
        assert r3["ok"] and r3["status"] == "rejected"
        cfg = await db.bot_configs.find_one({"user_id": uid})
        assert not cfg.get("kelly_enabled")
        # forbidden → rejected outright
        d4 = await propose_change(db, uid, "bridge_token", "a", "b",
                                  source="test")
        assert d4["status"] == "rejected"
        await db.bot_configs.delete_many({"user_id": uid})
        await db.governed_changes.delete_many({"user_id": uid})
    _run(go())
