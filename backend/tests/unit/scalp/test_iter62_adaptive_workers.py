"""Iter-62 — adaptive exits (safety envelope), worker separation, demo
history-cap exemption."""
import inspect

import pytest

from scalp import adaptive_exits as ax
from scalp.exec_quality import execution_quality

pytestmark = pytest.mark.unit
PIP = 0.0001


class TestSafetyEnvelope:
    def test_never_widens_buy(self):
        # proposed BELOW current stop (wider) → refused
        assert ax.clamp_tighter("BUY", 1.0800, 1.0790, 1.0850, PIP) is None

    def test_never_widens_sell(self):
        assert ax.clamp_tighter("SELL", 1.0900, 1.0950, 1.0850, PIP) is None

    def test_tighter_accepted_with_market_gap(self):
        new = ax.clamp_tighter("BUY", 1.0800, 1.0820, 1.0850, PIP)
        assert new == 1.0820
        # proposal too close to market → clamped to mid - 1p gap
        new = ax.clamp_tighter("BUY", 1.0800, 1.0851, 1.0850, PIP)
        assert new == pytest.approx(1.0850 - PIP)

    def test_equal_stop_is_not_tighter(self):
        assert ax.clamp_tighter("BUY", 1.0820, 1.0820, 1.0850, PIP) is None


class TestAdaptiveRules:
    def _base(self, **kw):
        args = dict(direction="BUY", entry_px=1.0850, mid=1.0851,
                    stop_px=1.0847, target_px=1.0855, pip_size=PIP,
                    elapsed_ms=10_000, max_holding_ms=120_000)
        args.update(kw)
        return ax.evaluate(**args)

    def test_hold_by_default(self):
        assert self._base()["action"] == "HOLD"

    def test_p_collapse_exits(self):
        act = self._base(p_target=0.20)
        assert act["action"] == "EXIT_NOW"
        assert act["reason"] == "adaptive_p_collapse"

    def test_p_collapse_ignored_when_near_target(self):
        act = self._base(p_target=0.20, mid=1.0854)   # 80% progress
        assert act["action"] == "HOLD"

    def test_regime_flip_exits(self):
        act = self._base(regime_opposes=True, mid=1.0850)
        assert act["action"] == "EXIT_NOW"
        assert act["reason"] == "adaptive_regime_flip"

    def test_vol_shock_tightens_to_breakeven_in_profit(self):
        act = self._base(vol_ratio=3.0)
        assert act["action"] == "TIGHTEN_STOP"
        assert act["reason"] == "adaptive_vol_shock"
        assert act["proposed_stop_px"] == 1.0850     # breakeven (in profit)

    def test_vol_shock_halves_stop_when_losing(self):
        act = self._base(vol_ratio=3.0, mid=1.0849)
        assert act["action"] == "TIGHTEN_STOP"
        assert act["proposed_stop_px"] == pytest.approx(1.08485)

    def test_spread_deterioration_only_in_profit(self):
        act = self._base(spread_pips=2.5, spread_limit=1.0)
        assert act["action"] == "TIGHTEN_STOP"
        assert act["reason"] == "adaptive_spread_deterioration"
        losing = self._base(spread_pips=2.5, spread_limit=1.0, mid=1.0848)
        assert losing["action"] == "HOLD"

    def test_time_decay_tightens(self):
        act = self._base(elapsed_ms=100_000, mid=1.08505)
        assert act["action"] == "TIGHTEN_STOP"
        assert act["reason"] == "adaptive_time_decay"


class TestEngineIntegration:
    def test_engine_wires_adaptive_manage(self):
        from scalp import engine
        src = inspect.getsource(engine)
        assert "def _adaptive_manage(" in src
        assert "adaptive_exits.clamp_tighter" in src
        assert "MODIFY_SL" in src
        # round 18 review item 2 — pending vs broker-confirmed stop state
        assert 'info.get("confirmed_stop_px")' in src
        assert "pending_stop_px" in src


class TestDemoHistoryExemption:
    def test_demo_live_exempt_from_cap(self):
        eq = execution_quality(spread_pctl=0.0, quote_age_ms=100,
                               avg_slippage_pips=0.05, ack_latency_ms=500,
                               vol_ratio=1.0, hour_utc=9,
                               broker_fill_count=0, history_cap_exempt=True)
        assert eq["score"] == 100 and eq["history_capped"] is False

    def test_live_still_capped(self):
        eq = execution_quality(broker_fill_count=0)
        assert eq["history_capped"] is True and eq["score"] <= 39


class TestWorkerSeparation:
    def test_server_gates_inprocess_loops(self):
        src = open("/app/backend/server.py").read()
        assert "BACKGROUND_WORKERS_IN_PROCESS" in src
        assert "from background_loops import" in src

    def test_background_loops_module_complete(self):
        import background_loops as bl
        for fn in ("_nightly_tuning_loop", "_optimizer_loop",
                   "_auto_heal_loop", "_scalp_reconcile_loop",
                   "_eod_flatten_loop", "_stuck_open_sync_loop"):
            assert callable(getattr(bl, fn))

    def test_worker_entry_points_exist(self):
        import workers.base as wb
        assert callable(wb.run_worker) and callable(wb.main)
        for mod in ("trading", "reconciliation", "tuning"):
            src = open(f"/app/backend/workers/{mod}.py").read()
            assert 'main(' in src

    # leader-lease DB test moved to tests/integration/scalp/
    # test_iter63_db_roundtrips.py (review item 1 — unit independence)
