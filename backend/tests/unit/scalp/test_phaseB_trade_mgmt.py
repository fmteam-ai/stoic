"""Phase B — Trade Management Engine: dynamic TP, continuous EV, intelligent
partials, volatility trailing, liquidity locks. Strictly risk-reducing.
MongoDB-free unit tests."""
import inspect

import pytest

from scalp import adaptive_exits as ax

pytestmark = pytest.mark.unit
PIP = 0.0001


def _eval(**kw):
    args = dict(direction="BUY", entry_px=1.0850, mid=1.0851,
                stop_px=1.0847, target_px=1.0855, pip_size=PIP,
                elapsed_ms=10_000, max_holding_ms=120_000)
    args.update(kw)
    return ax.evaluate(**args)


class TestContinuousEV:
    def test_hold_ev_r_math(self):
        # symmetric geometry at p=0.5 → EV 0
        assert ax.hold_ev_r(0.5, 5.0, 5.0) == pytest.approx(0.0)
        # barrier-consistent p (ds/(ds+dt)) → EV 0 by construction
        assert ax.hold_ev_r(7 / 8, 1.0, 7.0) == pytest.approx(0.0)
        assert ax.hold_ev_r(0.2, 1.0, 7.0) < -0.5

    def test_ev_exit_locks_gain_when_edge_gone(self):
        act = _eval(p_target=0.20, mid=1.0854)
        assert act["action"] == "EXIT_NOW"
        assert act["reason"] == "adaptive_ev_negative"
        assert act["ev_r"] < ax.EV_EXIT_R

    def test_barrier_consistent_p_holds_near_target(self):
        act = _eval(p_target=0.875, mid=1.0854)
        assert act["action"] == "HOLD"

    def test_ev_exit_never_fires_in_a_loser(self):
        # red position: EV exits only LOCK GAINS (p-collapse handles losers)
        act = _eval(p_target=0.40, mid=1.08495)
        assert act["reason"] != "adaptive_ev_negative"


class TestDynamicTP:
    def test_virtual_tp_hit_exits(self):
        act = _eval(mid=1.0855)
        assert act["action"] == "EXIT_NOW"
        assert act["reason"] == "adaptive_tp_hit"

    def test_tp_tightens_late_with_weak_p(self):
        act = _eval(p_target=0.40, entry_px=1.0850, stop_px=1.0847,
                    target_px=1.0860, mid=1.08521, elapsed_ms=70_000,
                    partial_done=True)
        assert act["action"] == "TIGHTEN_TP"
        # strictly CLOSER than the current target, never beyond
        assert 1.08521 < act["proposed_target_px"] < 1.0860

    def test_no_tp_surgery_when_nearly_there(self):
        act = _eval(p_target=0.40, target_px=1.0855, mid=1.08545,
                    elapsed_ms=70_000, partial_done=True)
        assert act["action"] != "TIGHTEN_TP"


class TestIntelligentPartials:
    KW = dict(entry_px=1.0850, stop_px=1.0847, target_px=1.0860,
              mid=1.08521)                       # +0.7R, 21% progress

    def test_partial_at_solid_gain_with_weak_p(self):
        act = _eval(p_target=0.40, **self.KW)
        assert act["action"] == "PARTIAL_CLOSE"
        assert act["fraction"] == ax.PARTIAL_FRACTION
        assert act["reason"] == "adaptive_partial_lock"

    def test_partial_on_vol_expansion(self):
        act = _eval(vol_ratio=2.2, **self.KW)
        assert act["action"] == "PARTIAL_CLOSE"

    def test_only_one_partial_per_trade(self):
        act = _eval(p_target=0.40, partial_done=True, **self.KW)
        assert act["action"] != "PARTIAL_CLOSE"

    def test_no_partial_with_healthy_p(self):
        act = _eval(p_target=0.65, **self.KW)
        assert act["action"] != "PARTIAL_CLOSE"

    def test_no_partial_below_trigger_gain(self):
        act = _eval(p_target=0.40, mid=1.0851)   # only +0.33R
        assert act["action"] != "PARTIAL_CLOSE"


class TestVolatilityTrailing:
    def test_trail_arms_at_half_r(self):
        act = _eval(vol_short_pips=2.0, entry_px=1.0850, stop_px=1.0847,
                    target_px=1.0860, mid=1.08521)   # +0.7R
        assert act["action"] == "TIGHTEN_STOP"
        assert act["reason"] == "adaptive_vol_trail"
        assert act["proposed_stop_px"] == pytest.approx(
            1.08521 - ax.TRAIL_VOL_MULT * 2.0 * PIP)

    def test_no_trail_below_start(self):
        act = _eval(vol_short_pips=2.0, mid=1.0851)  # +0.33R
        assert act["action"] == "HOLD"

    def test_trail_proposal_respects_envelope(self):
        # clamp refuses a proposal that would WIDEN the stop
        assert ax.clamp_tighter("BUY", 1.0849, 1.0848, 1.0852, PIP) is None


class TestLiquidityAwareExits:
    def test_liquidity_lock_in_profit(self):
        act = _eval(spread_pctl=0.95, mid=1.0851)    # +0.33R
        assert act["action"] == "TIGHTEN_STOP"
        assert act["reason"] == "adaptive_liquidity_lock"
        assert act["proposed_stop_px"] == 1.0850     # breakeven

    def test_no_liquidity_lock_when_red(self):
        act = _eval(spread_pctl=0.95, mid=1.0849)
        assert act["action"] == "HOLD"


class TestPhaseBWiring:
    def _src(self):
        from scalp import engine
        return inspect.getsource(engine)

    def test_engine_handles_new_actions(self):
        src = self._src()
        assert 'act["action"] == "TIGHTEN_TP"' in src
        assert 'act["action"] == "PARTIAL_CLOSE"' in src
        assert "adjusted_target_px" in src
        assert "adaptive_adjusted_tp" in src        # persisted + restored
        assert "def on_partial_ack(" in src
        # single pending-modification slot is respected
        assert src.count('"pending_modification": None') >= 2

    def test_partial_volume_respects_lot_step_and_min(self):
        src = self._src()
        assert "new_volume < self.cfg.min_lot or new_volume >= lot" in src

    def test_bridge_routes_partial_ack_hook(self):
        src = open("/app/backend/routes/bridge_routes.py").read()
        assert 'payload.type in ("MODIFY_SL", "PARTIAL_CLOSE")' in src
        assert "on_partial_ack(" in src

    def test_trade_manager_excludes_scalp_scope(self):
        src = open("/app/backend/trade_manager.py").read()
        assert 'trade.get("scope") == "scalp_fast"' in src

    def test_evaluate_priority_order(self):
        src = inspect.getsource(ax.evaluate)
        order = [src.index(s) for s in
                 ('"adaptive_tp_hit"', '"adaptive_p_collapse"',
                  '"adaptive_ev_negative"', '"PARTIAL_CLOSE"',
                  '"adaptive_vol_trail"', '"TIGHTEN_TP"')]
        assert order == sorted(order)
