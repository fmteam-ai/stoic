"""iter-85 · Dynamic sizing factors + adaptive daily risk budget."""
import asyncio
import pathlib
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).resolve().parents[1] / ".env")

from adaptive_sizing import exposure_mult, regime_mult, spread_mult  # noqa: E402
from database import get_db  # noqa: E402
from risk_budget import (DEFAULT_ALLOCATIONS, budget_status,  # noqa: E402
                         check_budget, strategy_class_of)

UID = "iter85-budget-user"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSizingMultipliers:
    def test_spread_tiers_fx_gold(self):
        assert spread_mult(1.0, "EURUSD") == 1.0
        assert spread_mult(2.5, "EURUSD") == 0.85
        assert spread_mult(5.0, "EURUSD") == 0.7
        assert spread_mult(9.0, "EURUSD") == 0.55
        assert spread_mult(3.0, "XAUUSD") == 1.0
        assert spread_mult(None, "EURUSD") == 1.0

    def test_regime_scaling(self):
        assert regime_mult("TREND_UP") == 1.1
        assert regime_mult("TREND_DOWN") == 1.1
        assert regime_mult("RANGE") == 0.9
        assert regime_mult("VOLATILITY_SHOCK") == 0.7
        assert regime_mult(None) == 1.0

    def test_exposure_shrinks_with_open_risk(self):
        assert exposure_mult(0, None) == 1.0
        assert exposure_mult(3, None) == 0.85
        assert exposure_mult(6, None) == 0.7
        assert exposure_mult(6, -4.0) == 0.56  # floating loss compounds
        assert exposure_mult(1, -1.0) == 1.0   # small floating loss ignored


class TestStrategyClassification:
    def test_scope_mapping(self):
        assert strategy_class_of("hf_scalp") == "scalp"
        assert strategy_class_of("scalp_fast") == "scalp"
        assert strategy_class_of(None) == "trend"
        assert strategy_class_of(None, "breakout") == "breakout"
        assert strategy_class_of("weird", "not_a_class") == "trend"


class TestRiskBudget:
    def test_default_allocations_sum_to_one(self):
        assert abs(sum(DEFAULT_ALLOCATIONS.values()) - 1.0) < 1e-9

    def test_status_shape(self):
        st = _run(budget_status(get_db(), UID, {}))
        assert st["pool_risk_pct"] == 3.0
        assert {r["strategy"] for r in st["strategies"]} == set(DEFAULT_ALLOCATIONS)

    def test_shrink_then_block_on_exhaustion(self):
        async def run():
            db = get_db()
            now = datetime.now(timezone.utc).isoformat()
            docs = [{"user_id": UID, "origin": "auto", "status": "open",
                     "strategy_class": "experimental", "risk_pct": 0.15,
                     "opened_at": now, "symbol": "EURUSD",
                     "label": "TEST_iter85"} for _ in range(2)]
            ids = (await db.trades.insert_many(docs)).inserted_ids
            try:
                # experimental budget = 3% × 10% = 0.30; spent 0.30 → blocked
                r = await check_budget(db, UID, {}, "experimental", 0.5)
                assert r["allowed"] is False, r
                assert "exhausted" in r["reason"]
                # other strategies unaffected — shrunk not disabled elsewhere
                r2 = await check_budget(db, UID, {}, "trend", 0.5)
                assert r2["allowed"] and r2["risk_pct"] == 0.5
            finally:
                await db.trades.delete_many({"_id": {"$in": ids}})
        _run(run())

    def test_partial_budget_shrinks_instead_of_blocking(self):
        async def run():
            db = get_db()
            now = datetime.now(timezone.utc).isoformat()
            ins = await db.trades.insert_one(
                {"user_id": UID, "origin": "auto", "status": "open",
                 "strategy_class": "trend", "risk_pct": 0.85,
                 "opened_at": now, "symbol": "EURUSD",
                 "label": "TEST_iter85"})
            try:
                # trend budget 1.05, spent 0.85 → remaining 0.20 ≥ floor
                r = await check_budget(db, UID, {}, "trend", 0.5)
                assert r["allowed"] and r.get("shrunk_from") == 0.5, r
                assert abs(r["risk_pct"] - 0.20) < 0.001
            finally:
                await db.trades.delete_one({"_id": ins.inserted_id})
        _run(run())

    def test_poor_performance_shrinks_allocation_not_disables(self):
        async def run():
            db = get_db()
            closed = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            docs = [{"user_id": UID, "origin": "auto", "status": "closed",
                     "strategy_class": "breakout", "pnl": -25.0,
                     "closed_at": closed, "opened_at": closed,
                     "symbol": "EURUSD", "label": "TEST_iter85"}
                    for _ in range(40)]  # rl_allocator needs ≥30 for authority
            ids = (await db.trades.insert_many(docs)).inserted_ids
            try:
                st = await budget_status(db, UID, {})
                row = next(r for r in st["strategies"]
                           if r["strategy"] == "breakout")
                assert row["perf_weight"] < 1.0, row       # shrunk…
                assert row["budget_risk_pct"] > 0, row     # …never disabled
            finally:
                await db.trades.delete_many({"_id": {"$in": ids}})
        _run(run())
