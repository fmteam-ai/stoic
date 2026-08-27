"""iter-205 Execution Intelligence — DATA-LEVEL integration tests (T1 review).

Complements tests/test_iter205_execution_intelligence.py (pure unit tests) by
exercising the real MongoDB against the actual modules for:

  * Broker Intelligence 2.0 execution_matrix — seeded 'auto' closed trades
    across 2 accounts with different slippage/latency profiles + failed
    trades → per-cell scores populated + broker_ranking orders the
    low-slippage broker above the high-slippage broker.
  * Pre-Trade Twin simulate — real db.pretrade_twin persistence + SKIP
    on edge<cost + REDUCE on oversized risk (gap budget breach) +
    decision_id passthrough.
  * Portfolio Risk Brain 2.0 marginal_verdict — vol_stress=1.0 (regime
    stress) produces stricter (<=) approved_fraction than vol_stress=0.0
    on the same correlated candidate; evaluate() carries factor_verdict
    and vol_stress keys; GOLD factor breach appears in blocks when
    candidate breaches 2.5% factor cap.
  * Uncertainty Engine 2.0 assess — thin history returns ADVISORY with
    components; seeded ≥20 alpha_clean closed trades returns components +
    conformal keys.

All test data is written under throwaway user_id/account_id
(TEST_iter205_*) and cleaned up in the module teardown fixture.
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import get_db  # noqa: E402
from broker_intel import execution_matrix  # noqa: E402
from pretrade_twin import simulate as twin_simulate  # noqa: E402
from portfolio_risk import (  # noqa: E402
    evaluate,
    marginal_factor_verdict,
)
from uncertainty_engine import assess as uncertainty_assess  # noqa: E402

TEST_UID = f"TEST_iter205_uid_{os.getpid()}"
THIN_UID = f"TEST_iter205_thin_{os.getpid()}"
RICH_UID = f"TEST_iter205_rich_{os.getpid()}"

ACC_GOOD = ObjectId()
ACC_BAD = ObjectId()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────── BROKER MATRIX ───────────────────────

def test_broker_matrix_ranks_good_above_bad():
    async def _t():
        db = get_db()
        # Two accounts, two distinct brokers
        await db.accounts.insert_many([
            {"_id": ACC_GOOD, "user_id": TEST_UID,
             "broker": "TEST_GoodBroker", "label": "good",
             "server": "Good-Server",
             "bridge_token": f"TEST_iter205_tok_good_{os.getpid()}"},
            {"_id": ACC_BAD, "user_id": TEST_UID,
             "broker": "TEST_BadBroker", "label": "bad",
             "server": "Bad-Server",
             "bridge_token": f"TEST_iter205_tok_bad_{os.getpid()}"},
        ])
        # Pin all timestamps inside ONE session window (london 07-12 UTC)
        # so fills and rejects always land in the same matrix cell.
        now = datetime.now(timezone.utc).replace(
            hour=10, minute=0, second=0, microsecond=0)
        docs = []
        # 10 closed 'auto' trades on GOOD account (tight slip, low latency)
        for i in range(10):
            docs.append({
                "user_id": TEST_UID,
                "account_id": str(ACC_GOOD),
                "origin": "auto", "status": "closed",
                "symbol": "XAUUSD", "action": "buy",
                "entry_price": 2000.0, "stop_loss": 1998.0,
                "exit_price": 2004.0, "pnl": 40.0,
                "slippage_pips": 0.2,
                "opened_at": (now - timedelta(hours=1, minutes=i))
                    .isoformat(),
                "created_at": (now - timedelta(hours=1, minutes=i))
                    .isoformat(),
                "closed_at": now.isoformat(),
                "latency_trace": {"t7_ms": 100, "t9_ms": 350},
            })
        # 10 closed trades on BAD account (bad slip, bad latency)
        for i in range(10):
            docs.append({
                "user_id": TEST_UID,
                "account_id": str(ACC_BAD),
                "origin": "auto", "status": "closed",
                "symbol": "XAUUSD", "action": "buy",
                "entry_price": 2000.0, "stop_loss": 1998.0,
                "exit_price": 2004.0, "pnl": 40.0,
                "slippage_pips": 10.0,
                "opened_at": (now - timedelta(hours=1, minutes=i))
                    .isoformat(),
                "created_at": (now - timedelta(hours=1, minutes=i))
                    .isoformat(),
                "closed_at": now.isoformat(),
                "latency_trace": {"t7_ms": 100, "t9_ms": 4500},
            })
        # 3 failed submissions on BAD account
        for i in range(3):
            docs.append({
                "user_id": TEST_UID,
                "account_id": str(ACC_BAD),
                "origin": "auto", "status": "failed",
                "symbol": "XAUUSD",
                "error": "10004 requote",
                "opened_at": now.isoformat(),
                "created_at": (now - timedelta(minutes=i)).isoformat(),
            })
        await db.trades.insert_many(docs)

        m = await execution_matrix(db, TEST_UID, days=30)
        assert m["days"] == 30
        assert m["cells"], f"expected cells: {m}"
        ranking = m["broker_ranking"]
        assert len(ranking) >= 2, ranking
        # Good broker must outrank bad broker
        names = [r["broker"] for r in ranking]
        assert names.index("TEST_GoodBroker") < names.index("TEST_BadBroker"), ranking
        # Good broker's cell should score materially higher
        good_cell = next(c for c in m["cells"]
                         if c["broker"] == "TEST_GoodBroker")
        bad_cell = next(c for c in m["cells"]
                        if c["broker"] == "TEST_BadBroker")
        assert good_cell["score"] > bad_cell["score"] + 10, (good_cell, bad_cell)
        assert bad_cell["reject_rate"] > 0
    _run(_t())


# ─────────────────────── PRE-TRADE TWIN ───────────────────────

def test_twin_skip_when_edge_below_cost_and_persists():
    async def _t():
        db = get_db()
        did = "TEST_iter205_did_skip"
        signal = {
            "symbol": "XAUUSD", "action": "buy",
            "entry_price": 2000.0, "stop_loss": 1998.0,
            "scope": "scalp",
            "decision_id": did,
            "calibrated_p_win": {"ev_r": 0.02, "p": 0.55},
            "meta_decision": {"transaction_cost": {"required_edge_r": 0.08}},
            "market_state": {"vector": {"spread_stress": 0.1}},
        }
        out = await twin_simulate(db, TEST_UID, signal, lot=0.1,
                                  equity=10000.0)
        assert out["verdict"] == "SKIP", out
        assert out["approved_fraction"] == 0.0
        # edge_vs_cost must be the failing check
        edge = next(c for c in out["checks"] if c["name"] == "edge_vs_cost")
        assert edge["passed"] is False
        # Persisted doc with decision_id passthrough
        doc = await db.pretrade_twin.find_one({"decision_id": did})
        assert doc is not None
        assert doc["verdict"] == "SKIP"
        assert doc["user_id"] == TEST_UID
    _run(_t())


def test_twin_reduce_when_gap_budget_breached():
    async def _t():
        db = get_db()
        did = "TEST_iter205_did_reduce"
        # Big lot so gap-shock (2× stop risk) exceeds 2% of equity budget.
        # entry=2000, stop=1998 → 2 pips gold ≈ 200 usd per lot on 1.0 lot
        # → 400 usd gap loss on 1.0 lot > 2% * 10000 = 200 → REDUCE.
        signal = {
            "symbol": "XAUUSD", "action": "buy",
            "entry_price": 2000.0, "stop_loss": 1998.0,
            "scope": "scalp",
            "decision_id": did,
            "calibrated_p_win": {"ev_r": 0.3, "p": 0.6},
            "meta_decision": {"transaction_cost": {"required_edge_r": 0.08}},
            "market_state": {"vector": {"spread_stress": 0.1}},
        }
        out = await twin_simulate(db, TEST_UID, signal, lot=1.0,
                                  equity=10000.0)
        assert out["verdict"] == "REDUCE", out
        assert 0.0 < out["approved_fraction"] < 1.0, out
        gap = next(c for c in out["checks"] if c["name"] == "gap_shock")
        assert gap["passed"] is False
    _run(_t())


# ─────────────────────── PORTFOLIO BRAIN 2.0 ───────────────────────

def test_evaluate_carries_factor_verdict_and_vol_stress():
    ev = evaluate([], {"symbol": "XAUUSD", "action": "BUY", "lot": 0.01,
                       "entry_price": 2000.0, "stop_loss": 1998.0},
                  equity=100000, vol_stress=0.8)
    assert "factor_verdict" in ev
    assert ev["vol_stress"] == 0.8


def test_marginal_factor_verdict_breach_reduces():
    positions = [{"symbol": "XAUUSD", "action": "BUY", "lot": 1.0,
                  "entry_price": 2000.0, "stop_loss": 1998.0}]
    candidate = {"symbol": "XAUUSD", "action": "BUY", "lot": 2.0,
                 "entry_price": 2000.0, "stop_loss": 1998.0}
    v = marginal_factor_verdict(positions, candidate, equity=10000)
    assert v["breaches"], v
    assert any("GOLD" in b for b in v["breaches"]), v
    assert v["approved_fraction"] < 1.0


def test_evaluate_vol_stress_tightens_correlations():
    """With correlated open positions, vol_stress=1.0 should produce a
    stricter (>=blocks or equal) portfolio verdict than vol_stress=0.0."""
    positions = [
        {"symbol": "EURUSD", "action": "BUY", "lot": 0.5,
         "entry_price": 1.10, "stop_loss": 1.099},
        {"symbol": "GBPUSD", "action": "BUY", "lot": 0.5,
         "entry_price": 1.25, "stop_loss": 1.249},
    ]
    candidate = {"symbol": "AUDUSD", "action": "BUY", "lot": 0.5,
                 "entry_price": 0.65, "stop_loss": 0.649}
    ev_calm = evaluate(positions, candidate, equity=10000,
                       vol_stress=0.0)
    ev_stressed = evaluate(positions, candidate, equity=10000,
                           vol_stress=1.0)
    # cluster risk should be >= under stress (correlations tightened)
    assert ev_stressed["cluster_risk_usd"] >= ev_calm["cluster_risk_usd"]
    # if calm blocks, stress must also block (never looser)
    if not ev_calm["ok"]:
        assert not ev_stressed["ok"]


# ─────────────────────── UNCERTAINTY 2.0 ───────────────────────

def test_uncertainty_thin_history_advisory_with_components():
    async def _t():
        db = get_db()
        out = await uncertainty_assess(
            db, THIN_UID,
            {"symbol": "XAUUSD", "scope": "ai",
             "confidence": 60,
             "calibrated_p_win": {"p": 0.55},
             "market_state": {"available": True,
                              "vector": {"spread_stress": 0.2}}})
        assert out["decision"] == "ADVISORY"
        assert out["hard_gate"] is False
        assert "components" in out
        assert "regime_uncertainty" in out["components"]
        assert "execution_uncertainty" in out["components"]
    _run(_t())


def test_uncertainty_rich_history_returns_conformal_and_components():
    async def _t():
        db = get_db()
        now = datetime.now(timezone.utc)
        docs = []
        for i in range(25):
            is_win = i % 2 == 0
            docs.append({
                "user_id": RICH_UID, "scope": "ai",
                "symbol": "XAUUSD", "action": "buy",
                "status": "closed",
                "entry_price": 2000.0, "stop_loss": 1998.0,
                "exit_price": 2003.0 if is_win else 1998.0,
                "pnl": 30.0 if is_win else -20.0,
                "closed_at": (now - timedelta(days=i)).isoformat(),
                "alpha_clean": True,
            })
        await db.trades.insert_many(docs)
        out = await uncertainty_assess(
            db, RICH_UID,
            {"symbol": "XAUUSD", "scope": "ai",
             "confidence": 60,
             "calibrated_p_win": {"p": 0.55},
             "market_state": {"available": True,
                              "vector": {"spread_stress": 0.2}}})
        assert "conformal" in out, out
        assert "components" in out
        comps = out["components"]
        for k in ("bootstrap_width", "disagreement",
                  "conformal_uncertainty", "regime_uncertainty",
                  "execution_uncertainty"):
            assert k in comps, comps
        assert out["decision"] in ("TRADE", "SKIP")
        assert out["n"] >= 20
    _run(_t())


# ─────────────────────── CLEANUP ───────────────────────

@pytest.fixture(scope="module", autouse=True)
def _cleanup_test_data():
    yield
    async def _c():
        db = get_db()
        for uid in (TEST_UID, THIN_UID, RICH_UID):
            await db.trades.delete_many({"user_id": uid})
            await db.pretrade_twin.delete_many({"user_id": uid})
            await db.signals.delete_many({"user_id": uid})
        await db.accounts.delete_many({"_id": {"$in": [ACC_GOOD, ACC_BAD]}})
    try:
        _run(_c())
    except Exception:
        pass


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
