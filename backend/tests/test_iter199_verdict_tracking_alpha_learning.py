"""iter-199 — Verdict Outcome Tracking + alpha-clean AI learning.
1) Every REDUCE/REJECT risk verdict is recorded and scored against its
   real (linked trade) or counterfactual (price-path) outcome.
2) learned_meta._build_dataset feeds ONLY alpha-clean outcomes into
   model learning — noise-dominated trades never retrain the strategy."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


TAG = f"vt199-{uuid.uuid4().hex[:6]}"


def _cleanup():
    db = _db()
    _run(db.risk_verdicts.delete_many({"user_id": TAG}))
    _run(db.price_ticks.delete_many({"symbol": "VT199TEST"}))
    _run(db.trades.delete_many({"user_id": TAG}))
    _run(db.signals.delete_many({"vt199": TAG}))


class TestVerdictRecordAndResolve:
    def teardown_method(self):
        _cleanup()

    def test_reduce_verdict_scored_against_real_trade_loss(self):
        """Reduction on a losing trade → money saved = the loss NOT taken."""
        from verdict_tracking import (link_trade, record_verdict,
                                      resolve_with_trade)
        db = _db()
        vid = _run(record_verdict(
            db, source="pamm_verdict", verdict="REDUCE", requested=1.0,
            approved=0.5, unit="risk_pct", user_id=TAG, nav=10_000,
            limiting_factor="daily_loss"))
        trade = {"_id": f"tr-{TAG}-loss", "user_id": TAG, "pnl": -50.0,
                 "status": "closed"}
        _run(link_trade(db, vid, trade["_id"]))
        res = _run(resolve_with_trade(db, trade))
        assert res is not None
        # full size would have lost $100 → reduction saved $50
        assert res["hypothetical_full_pnl"] == -100.0
        assert res["saved_usd"] == 50.0
        assert res["opportunity_cost_usd"] == 0.0
        assert res["net_benefit_usd"] == 50.0
        doc = _run(db.risk_verdicts.find_one({"verdict_id": vid}))
        assert doc["status"] == "resolved"
        assert doc["resolution"]["kind"] == "real"

    def test_reduce_verdict_scored_against_real_trade_win(self):
        """Reduction on a winning trade → opportunity cost, not savings."""
        from verdict_tracking import (link_trade, record_verdict,
                                      resolve_with_trade)
        db = _db()
        vid = _run(record_verdict(
            db, source="trading_authority", verdict="REDUCE",
            requested=0.2, approved=0.1, unit="lot", user_id=TAG))
        trade = {"_id": f"tr-{TAG}-win", "user_id": TAG, "pnl": 40.0,
                 "status": "closed"}
        _run(link_trade(db, vid, trade["_id"]))
        res = _run(resolve_with_trade(db, trade))
        assert res["hypothetical_full_pnl"] == 80.0
        assert res["saved_usd"] == 0.0
        assert res["opportunity_cost_usd"] == 40.0
        assert res["net_benefit_usd"] == -40.0

    def test_blocked_verdict_counterfactual_stop_hit_saves_money(self):
        """Blocked BUY whose price path hits the stop → block saved money."""
        from verdict_tracking import record_verdict, resolve_blocked
        db = _db()
        vid = _run(record_verdict(
            db, source="pamm_verdict", verdict="REJECT", requested=1.0,
            approved=0.0, unit="risk_pct", user_id=TAG, nav=10_000,
            limiting_factor="drawdown",
            context={"symbol": "VT199TEST", "side": "BUY",
                     "entry_price": 100.0, "stop_loss": 95.0,
                     "take_profit": 110.0}))
        now = datetime.now(timezone.utc)
        _run(db.price_ticks.insert_many([
            {"ts": now + timedelta(seconds=1), "symbol": "VT199TEST",
             "price": 99.0},
            {"ts": now + timedelta(seconds=2), "symbol": "VT199TEST",
             "price": 94.5},   # stop crossed
        ]))
        n = _run(resolve_blocked(db, limit=10))
        assert n >= 1
        doc = _run(db.risk_verdicts.find_one({"verdict_id": vid}))
        assert doc["status"] == "resolved"
        res = doc["resolution"]
        assert res["kind"] == "counterfactual_stop"
        assert res["result_r"] == -1.0
        # 1% of 10k NAV = $100 risk → block saved $100
        assert res["saved_usd"] == 100.0
        assert res["net_benefit_usd"] == 100.0

    def test_blocked_verdict_counterfactual_target_hit_costs(self):
        """Blocked SELL whose price path hits the target → opportunity cost."""
        from verdict_tracking import record_verdict, resolve_blocked
        db = _db()
        vid = _run(record_verdict(
            db, source="pamm_verdict", verdict="REJECT", requested=2.0,
            approved=0.0, unit="risk_pct", user_id=TAG, nav=5_000,
            context={"symbol": "VT199TEST", "side": "SELL",
                     "entry_price": 100.0, "stop_loss": 105.0,
                     "take_profit": 90.0}))
        now = datetime.now(timezone.utc)
        _run(db.price_ticks.insert_many([
            {"ts": now + timedelta(seconds=1), "symbol": "VT199TEST",
             "price": 96.0},
            {"ts": now + timedelta(seconds=2), "symbol": "VT199TEST",
             "price": 89.0},   # target crossed (SELL)
        ]))
        _run(resolve_blocked(db, limit=10))
        doc = _run(db.risk_verdicts.find_one({"verdict_id": vid}))
        res = doc["resolution"]
        assert res["kind"] == "counterfactual_target"
        assert res["result_r"] == 2.0   # 10 reward / 5 risk
        # 2% of 5k = $100 risk × 2R = $200 missed
        assert res["opportunity_cost_usd"] == 200.0
        assert res["net_benefit_usd"] == -200.0

    def test_blocked_verdict_without_ticks_stays_pending(self):
        from verdict_tracking import record_verdict, resolve_blocked
        db = _db()
        vid = _run(record_verdict(
            db, source="pamm_verdict", verdict="REJECT", requested=1.0,
            approved=0.0, user_id=TAG,
            context={"symbol": "VT199TEST", "side": "BUY",
                     "entry_price": 100.0, "stop_loss": 95.0}))
        _run(resolve_blocked(db, limit=10))
        doc = _run(db.risk_verdicts.find_one({"verdict_id": vid}))
        assert doc["status"] == "pending"   # inside 48h window, no data yet

    def test_effectiveness_summary_aggregates(self):
        from verdict_tracking import (effectiveness_summary, link_trade,
                                      record_verdict, resolve_with_trade)
        db = _db()
        vid = _run(record_verdict(
            db, source="pamm_verdict", verdict="REDUCE", requested=1.0,
            approved=0.25, unit="risk_pct", user_id=TAG,
            limiting_factor="weekly_loss"))
        trade = {"_id": f"tr-{TAG}-agg", "user_id": TAG, "pnl": -25.0,
                 "status": "closed"}
        _run(link_trade(db, vid, trade["_id"]))
        _run(resolve_with_trade(db, trade))
        summ = _run(effectiveness_summary(db, days=1, user_id=TAG))
        assert summ["totals"]["verdicts"] == 1
        assert summ["totals"]["resolved"] == 1
        assert summ["totals"]["saved_usd"] == 75.0
        assert "weekly_loss" in summ["by_limiting_factor"]
        assert summ["by_limiting_factor"]["weekly_loss"][
            "net_benefit_usd"] == 75.0


class TestAlphaCleanLearning:
    def teardown_method(self):
        _cleanup()

    def _seed_trade(self, alpha_clean, pnl, primary=None):
        db = _db()
        sig = {"vt199": TAG, "confidence": 70, "action": "BUY",
               "entry_price": 2400.0, "created_at":
               datetime.now(timezone.utc).isoformat()}
        sid = _run(db.signals.insert_one(sig)).inserted_id
        doc = {"user_id": TAG, "status": "closed", "signal_id": str(sid),
               "pnl": pnl, "symbol": "XAUUSD", "entry_price": 2400.0,
               "opened_at": datetime.now(timezone.utc).isoformat()}
        if alpha_clean is not None:
            doc["alpha_clean"] = alpha_clean
            doc["attribution_primary"] = primary or (
                "NORMAL_VARIANCE" if alpha_clean else "BROKER_ERROR")
        _run(db.trades.insert_one(doc))

    def test_noise_dominated_trades_never_reach_the_model(self):
        """alpha_clean=False trades are filtered from the training set and
        counted by their primary noise category."""
        from learned_meta import _build_dataset
        self._seed_trade(True, 30.0)
        self._seed_trade(True, -20.0)
        self._seed_trade(False, -80.0, primary="BROKER_ERROR")
        self._seed_trade(False, -60.0, primary="INFRASTRUCTURE_ERROR")
        self._seed_trade(None, 10.0)   # legacy unattributed → included
        X, y, sessions, sw, quality = _run(_build_dataset())
        mine_excluded = quality["excluded_by_category"]
        assert quality["excluded_noise"] >= 2
        assert mine_excluded.get("BROKER_ERROR", 0) >= 1
        assert mine_excluded.get("INFRASTRUCTURE_ERROR", 0) >= 1
        assert quality["accepted"] >= 3
        assert quality["alpha_clean_accepted"] >= 2
        assert quality["unattributed_included"] >= 1
        # the -80/-60 broker-noise losses are NOT in the label set for
        # this user's seeded trades: total samples = accepted count
        assert len(y) == quality["accepted"]

    def test_quality_persisted_for_api(self):
        """retrain() persists the learning diet even when undertrained."""
        from learned_meta import retrain
        db = _db()
        _run(db.learning_quality.delete_many({"_id": "last"}))
        out = _run(retrain())
        assert "learning_quality" in out
        doc = _run(db.learning_quality.find_one({"_id": "last"}))
        assert doc is not None
        assert "accepted" in doc and "excluded_noise" in doc


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
