"""iter-90 — Phase 3 multi-strategy portfolio: per-strategy metrics
(expected return, volatility, drawdown, correlation, capacity, confidence)
and dynamic capital allocation feeding the daily risk budget."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from strategy_portfolio import (build_allocation, current_allocations,
                                strategy_metrics, _corr, _max_drawdown)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter90-{uuid.uuid4().hex[:8]}"


async def _seed(db, cls, pnls, symbol="XAUUSD", scope=None):
    for i, p in enumerate(pnls):
        await db.trades.insert_one({
            "user_id": UID, "origin": "auto", "status": "closed",
            "pnl": float(p), "symbol": symbol, "strategy_class": cls,
            **({"scope": scope} if scope else {}),
            "tp_pips": [40.0, 80.0], "entry_price": 4000.0,
            "closed_at": (datetime.now(timezone.utc)
                          - timedelta(days=(i % 14) + 1)).isoformat()})


async def _cleanup(db):
    await db.trades.delete_many({"user_id": UID})
    await db.strategy_allocations.delete_many({"user_id": UID})


# ------------------------------------------------------------ pure maths
def test_corr_and_drawdown():
    a = [1, 2, 3, 4, 5, 6]
    assert _corr(a, a) == 1.0
    assert _corr(a, [-x for x in a]) == -1.0
    assert _corr(a, [1, 2]) is None
    assert _max_drawdown([10, -5, -10, 20]) == 15.0
    assert _max_drawdown([1, 1, 1]) == 0.0


def test_build_allocation_shifts_to_winner():
    mk = lambda sharpe, conf, cap=80.0, corr=0.0: {
        "sharpe_daily": sharpe, "avg_pos_correlation": corr,
        "capacity": {"score": cap}, "confidence": {"score": conf}}
    metrics = {"trend": mk(1.5, 0.9), "scalp": mk(-0.5, 0.9),
               "breakout": mk(0.1, 0.3), "mean_reversion": mk(0.4, 0.5),
               "experimental": mk(0.0, 0.1)}
    w = build_allocation(metrics)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["trend"] > w["scalp"]           # winner gets more
    assert w["trend"] <= 0.55                # clamp respected (post-norm)
    assert all(v > 0.03 for v in w.values())  # floor — never starved


def test_build_allocation_low_confidence_stays_near_base():
    mk = lambda sharpe, conf: {
        "sharpe_daily": sharpe, "avg_pos_correlation": 0.0,
        "capacity": {"score": 80.0}, "confidence": {"score": conf}}
    metrics = {k: mk(2.0 if k == "experimental" else 0.0, 0.05)
               for k in ("trend", "scalp", "breakout", "mean_reversion",
                         "experimental")}
    w = build_allocation(metrics)
    # tiny confidence → experimental can't hijack the pool despite sharpe 2
    assert w["experimental"] < w["trend"]


def test_correlation_penalty():
    mk = lambda corr: {"sharpe_daily": 1.0, "avg_pos_correlation": corr,
                       "capacity": {"score": 80.0},
                       "confidence": {"score": 1.0}}
    metrics = {"trend": mk(0.0), "scalp": mk(0.9),
               "breakout": mk(0.0), "mean_reversion": mk(0.0),
               "experimental": mk(0.0)}
    w = build_allocation(metrics)
    assert w["scalp"] < w["trend"]  # correlated strategy penalized


# ------------------------------------------------------------ end-to-end
def test_strategy_metrics_e2e(db):
    async def run():
        await _cleanup(db)
        await _seed(db, "trend", [100, 120, -40, 90, 110, 80, -30, 95,
                                  105, 70, 60, -20, 85, 90])
        await _seed(db, None, [-15, -20, 10, -18, -25, -12, 8, -22],
                    scope="scalp_fast", symbol="BTCUSD")
        m = await strategy_metrics(db, UID)
        tr, sc = m["strategies"]["trend"], m["strategies"]["scalp"]
        assert tr["n_trades"] == 14 and sc["n_trades"] == 8
        assert tr["expected_return_usd_day"] > 0
        assert sc["expectancy_usd_trade"] < 0
        assert tr["volatility_usd_day"] > 0
        assert tr["max_drawdown_usd"] >= 0
        assert 0 <= tr["confidence"]["score"] <= 1
        assert 0 <= tr["capacity"]["score"] <= 100
        assert m["by_asset"]["gold"]["trades"] == 14
        assert m["by_asset"]["crypto"]["trades"] == 8
        await _cleanup(db)
    _run(run())


def test_current_allocations_cache_and_threshold(db):
    async def run():
        await _cleanup(db)
        # <10 trades → None (static)
        await _seed(db, "trend", [50, 60, 70])
        assert await current_allocations(db, UID, {}) is None
        # enough evidence → dynamic + cached
        await _seed(db, "trend", [80, 90, -30, 100, 40, 60, 75])
        await _seed(db, "scalp", [-10, -20, -15, -12])
        dyn = await current_allocations(db, UID, {})
        assert dyn and dyn["basis"] == "dynamic"
        w = dyn["weights"]
        assert abs(sum(w.values()) - 1.0) < 1e-3
        assert w["trend"] > w["scalp"]
        cached = await db.strategy_allocations.find_one({"user_id": UID})
        assert cached and cached["weights"] == w
        await _cleanup(db)
    _run(run())


def test_budget_status_uses_dynamic_allocation(db):
    from risk_budget import budget_status

    async def run():
        await _cleanup(db)
        await _seed(db, "trend", [100, 90, 80, 110, -40, 95, 85, 70, 60, 105,
                                  90, -25])
        st = await budget_status(db, UID, {"dynamic_allocation_enabled": True})
        assert st["allocation_basis"] == "dynamic"
        st2 = await budget_status(db, UID, {"dynamic_allocation_enabled": False})
        assert st2["allocation_basis"] == "static"
        trend2 = next(r for r in st2["strategies"] if r["strategy"] == "trend")
        assert trend2["allocation_pct"] == 35.0  # static base share
        await _cleanup(db)
    _run(run())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
