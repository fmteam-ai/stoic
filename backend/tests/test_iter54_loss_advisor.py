"""iter-54 · Auto Loss Review — aggregate loss analysis with shadow-tested
counter-measures (loss_advisor.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loss_advisor import _predicate, _shadow_test, _crossed_weekend, _aggregates  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ds(trades, signals=None):
    return {"trades": trades, "signals": signals or {}, "losses": [t for t in trades if (t.get("pnl") or 0) < 0],
            "since_loss": "2026-06-29", "since_shadow": "2026-06-22"}


T = lambda pnl, sym="XAUUSD", act="SELL", sid=None, opened="2026-07-06T08:00:00+00:00", closed="2026-07-06T10:00:00+00:00": {  # noqa: E731
    "pnl": pnl, "symbol": sym, "action": act, "signal_id": sid,
    "opened_at": opened, "closed_at": closed,
}


class TestPredicates:
    def test_min_confidence(self):
        pred = _predicate({"type": "min_confidence", "params": {"value": 75}})
        assert pred(T(-10), {"confidence": 70}) is True
        assert pred(T(-10), {"confidence": 80}) is False
        assert pred(T(-10), {}) is False  # no confidence → not blocked

    def test_min_confidence_symbol_scoped(self):
        pred = _predicate({"type": "min_confidence", "params": {"value": 75, "symbol": "XAUUSD"}})
        assert pred(T(-10, sym="GOLD#"), {"confidence": 70}) is True   # suffix-aware
        assert pred(T(-10, sym="US30"), {"confidence": 70}) is False

    def test_velocity_veto_measure(self):
        pred = _predicate({"type": "velocity_veto", "params": {
            "regime": "LOW_VOL_TREND", "velocity_counter_max": 3.0, "velocity_veto_threshold": 5.0}})
        sig = {"action": "SELL", "regime": {"regime": "LOW_VOL_TREND"},
               "indicators": {"kalman_filter": {"k_velocity": 7.9}}, "mtf_gate": {"htf_trend": "DOWN"}}
        assert pred(T(-10), sig) is True
        sig2 = {**sig, "indicators": {"kalman_filter": {"k_velocity": 1.0}}}
        assert pred(T(-10), sig2) is False

    def test_session_block(self):
        pred = _predicate({"type": "session_block", "params": {"session": "asia"}})
        assert pred(T(-10), {"session": {"primary": "ASIA"}}) is True
        assert pred(T(-10), {"session": {"primary": "london"}}) is False

    def test_symbol_pause(self):
        pred = _predicate({"type": "symbol_pause", "params": {"symbol": "US30"}})
        assert pred(T(-10, sym="US30.cash"), {}) is True
        assert pred(T(-10, sym="XAUUSD"), {}) is False

    def test_friday_flat_measure(self):
        pred = _predicate({"type": "friday_flat", "params": {"mode": "close"}})
        assert pred(T(-10, opened="2026-07-03T15:00:00+00:00", closed="2026-07-05T22:00:00+00:00"), {}) is True
        assert pred(T(-10), {}) is False

    def test_other_not_testable(self):
        assert _predicate({"type": "other", "params": {}}) is None


class TestShadowMath:
    def test_net_effect(self):
        trades = [T(-100, sym="US30"), T(-50, sym="US30"), T(30, sym="US30"), T(80, sym="XAUUSD")]
        ev = _shadow_test({"type": "symbol_pause", "params": {"symbol": "US30"}}, ds(trades))
        assert ev["trades_blocked"] == 3
        assert ev["losses_avoided"] == 150.0
        assert ev["wins_missed"] == 30.0
        assert ev["net_effect"] == 120.0

    def test_untestable_returns_none(self):
        assert _shadow_test({"type": "other", "params": {}}, ds([T(-10)])) is None


class TestWeekendCross:
    def test_crossed(self):
        assert _crossed_weekend(T(-10, opened="2026-07-03T15:00:00+00:00",
                                  closed="2026-07-05T22:00:00+00:00")) is True

    def test_not_crossed(self):
        assert _crossed_weekend(T(-10)) is False


class TestAggregates:
    def test_basic(self):
        trades = [T(-100), T(50), T(-20)]
        agg = _aggregates(ds(trades))
        assert agg["trades"] == 3 and agg["losses"] == 2
        assert agg["total_pnl"] == -70.0 and agg["loss_pnl"] == -120.0


class TestWiring:
    def test_bot_runner_sweep(self):
        src = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "sweep_loss_reviews(db)" in src

    def test_routes_registered_before_trade_id(self):
        src = open(os.path.join(BACKEND, "routes", "postmortem_routes.py")).read()
        assert src.index('@router.get("/reviews")') < src.index('@router.get("/{trade_id}")')
        assert '@router.post("/reviews/run")' in src

    def test_frontend_section(self):
        fe = open("/app/frontend/src/pages/LossLab.jsx").read()
        assert "run-loss-review" in fe and "loss-review-card" in fe

    def test_auto_learning_wired(self):
        # iter-55: auto-apply/auto-revert superseded the "never auto-applied" rule
        src = open(os.path.join(BACKEND, "loss_advisor.py")).read()
        assert "_auto_apply" in src and "_revalidate_guards" in src
        assert "live_guard_block" in src
        runner = open(os.path.join(BACKEND, "bot_runner.py")).read()
        assert "live_guard_block" in runner and "auto_guard_block" in runner
