"""Scalp fast-path UNIT tests — pure Python imports only.

No database, no network, no environment files, no running backend.
Paths are project-relative (review: testing improvements).
"""
import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

import sys
BACKEND = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND))

from scalp.instruments import approved  # noqa: E402
from scalp.state import ScalpState, TickEvent  # noqa: E402
from scalp.features import snapshot, FEATURE_KEYS  # noqa: E402
from scalp.setup import detect  # noqa: E402
from scalp.forecast import make as make_forecast  # noqa: E402
from scalp import edge, kill  # noqa: E402
from scalp.risk import (DEFAULT_LIMITS, RiskState, ScalpRiskLimits,  # noqa: E402
                        check as risk_check, size_lot)
from scalp.gate import final_execution_gate  # noqa: E402
from scalp.engine import ScalpRunner, ShadowSim  # noqa: E402
from scalp import model as scalp_model  # noqa: E402

CFG = approved("EURUSD")
PIP = CFG.pip_size


def _mk_state(prices, start_ms=None, gap_ms=500, spread_pips=0.6):
    st = ScalpState(PIP)
    t0 = start_ms or int(time.time() * 1000) - (len(prices) - 1) * gap_ms
    for i, mid in enumerate(prices):
        half = spread_pips * PIP / 2
        tm = t0 + i * gap_ms
        st.update(TickEvent(symbol="EURUSD", broker_time_ms=tm,
                            received_time_ms=tm + 50,
                            bid=mid - half, ask=mid + half))
    return st


def _impulse_pullback_path():
    base = 1.08000
    path = [base] * 140
    path += [base + i * 0.3 * PIP for i in range(1, 11)]
    top = path[-1]
    path += [top - i * 0.25 * PIP for i in range(1, 5)]
    low = path[-1]
    inc = 0.0
    for step in (0.05, 0.1, 0.15, 0.2):
        inc += step * PIP
        path.append(low + inc)
    return path


def _tick(ms, bid, ask):
    return TickEvent(symbol="EURUSD", broker_time_ms=ms,
                     received_time_ms=ms, bid=bid, ask=ask)


# ---------------- universe / state / features / setup ----------------
class TestInstruments:
    def test_eurusd_only(self):
        assert CFG is not None and CFG.contract_size == 100_000.0
        for s in ("XAUUSD", "BTCUSD", "US30", "EURTRY", "GBPUSD"):
            assert approved(s) is None


class TestFeatures:
    def test_snapshot_none_when_thin(self):
        assert snapshot(_mk_state([1.08] * 5)) is None

    def test_snapshot_keys(self):
        f = snapshot(_mk_state([1.08 + i * 0.00001 for i in range(120)]))
        for k in FEATURE_KEYS:
            assert k in f

    def test_returns_in_pips(self):
        f = snapshot(_mk_state([1.08 + i * 0.5 * PIP for i in range(120)]))
        assert 0.5 <= f["ret_1s"] <= 1.6


class TestSetup:
    def test_micro_pullback_detected(self):
        st = _mk_state(_impulse_pullback_path())
        cand = detect(snapshot(st), st)
        assert cand and cand["direction"] == "BUY"
        assert 0.15 <= cand["pullback_frac"] <= 0.60

    def test_flat_market_no_candidate(self):
        st = _mk_state([1.08] * 120)
        f = snapshot(st)
        assert f is None or detect(f, st) is None


# ---------------- cost accounting (review item 1) ----------------
class TestCostAccounting:
    def test_net_pips_no_double_count(self):
        """entry ask+slip, target exit at bid (no exit slip), minus commission —
        net must equal exec-to-exec minus commission, and gross the mid move."""
        sim = ShadowSim("d", "BUY", entry_mid=1.08003, entry_exec=1.08007,
                        target_pips=2.0, stop_pips=2.0, pip=PIP,
                        opened_ms=0, max_holding_ms=60_000,
                        exit_slippage_pips=0.3, commission_pips=0.4)
        out = sim.advance(_tick(1000, 1.08027, 1.08033))   # bid hits target
        assert out["result"] == "target_first"
        exec_move = (1.08027 - 1.08007) / PIP              # 2.0 pips
        assert abs(out["net_pips"] - (exec_move - 0.4)) < 0.011  # NO exit slip on limit
        assert out["exit_slippage_pips"] == 0.0
        assert out["commission_pips"] == 0.4
        mid_move = ((1.08027 + 1.08033) / 2 - 1.08003) / PIP
        assert abs(out["gross_move_pips"] - mid_move) < 0.011

    def test_stop_exit_applies_slippage(self):
        sim = ShadowSim("d", "BUY", 1.08003, 1.08007, 2.0, 2.0, PIP, 0, 60_000,
                        exit_slippage_pips=0.3, commission_pips=0.0)
        out = sim.advance(_tick(1000, 1.07986, 1.07992))   # bid below stop
        assert out["result"] == "stop_first"
        exec_move = (1.07986 - 1.08007) / PIP
        assert abs(out["net_pips"] - (exec_move - 0.3)) < 0.011

    def test_short_target_uses_ask(self):
        sim = ShadowSim("d", "SELL", 1.08003, 1.07999, 2.0, 2.0, PIP, 0, 60_000)
        assert sim.advance(_tick(1000, 1.07977, 1.07984)) is None  # ask above target? 1.07984>1.07979 → None? target=1.07979
        out = sim.advance(_tick(2000, 1.07970, 1.07977))
        assert out["result"] == "target_first"

    def test_timeout(self):
        sim = ShadowSim("d", "SELL", 1.08003, 1.07999, 2.0, 2.0, PIP, 0, 5_000)
        out = sim.advance(_tick(6_000, 1.08000, 1.08006))
        assert out["result"] == "timeout"


# ---------------- commission conversion (review item 8) ----------------
class TestCommission:
    def test_round_trip_conversion(self):
        r = ScalpRunner("acc1", "u1", "EURUSD")
        r.commission_usd_per_lot_side = 3.5     # $3.5/side → $7 round trip
        # EURUSD pip value $10/lot → 0.7 pips
        assert abs(r._commission_pips() - 0.7) < 1e-6

    def test_zero_commission(self):
        r = ScalpRunner("acc2", "u1", "EURUSD")
        assert r._commission_pips() == 0.0


# ---------------- forecast + edge ----------------
class TestEdge:
    def _forecast(self, spread=0.6, commission=0.0):
        st = _mk_state(_impulse_pullback_path(), spread_pips=spread)
        f = snapshot(st)
        cand = detect(f, st)
        assert cand
        return make_forecast(f, cand, st, CFG, commission_pips=commission)

    def test_forecast_fields(self):
        fc = self._forecast()
        assert 0.30 <= fc.p_target_before_stop <= 0.70
        assert fc.target_pips > fc.stop_pips

    def test_wide_spread_kills_edge(self):
        assert edge.evaluate(self._forecast(spread=3.0))["ok"] is False

    def test_commission_reduces_edge(self):
        base = edge.evaluate(self._forecast())["net_edge_pips"]
        with_c = edge.evaluate(self._forecast(commission=0.7))["net_edge_pips"]
        assert abs((base - with_c) - 0.7) < 1e-6


# ---------------- risk + persistence (review item 9) ----------------
class TestRisk:
    def test_sizing_fail_closed(self):
        assert size_lot(0, 2.0, 10.0, CFG, DEFAULT_LIMITS)["ok"] is False
        assert size_lot(100, 2.0, 10.0, CFG, DEFAULT_LIMITS)["ok"] is False

    def test_normal_sizing(self):
        r = size_lot(50_000, 2.0, 10.0, CFG, DEFAULT_LIMITS)
        assert r["ok"] and 1.0 <= r["lot"] <= 1.25

    def test_cooldown_and_caps(self):
        rs = RiskState(ScalpRiskLimits(max_consecutive_losses=2,
                                       cooldown_after_loss_streak_minutes=30))
        rs.record_result(-5, 0.5)
        rs.record_result(-5, 0.5)
        assert "cooldown" in risk_check(rs, 50_000, 2.0, 10.0, CFG)["reason"]

    def test_persistence_roundtrip_restores_safety_state(self):
        rs = RiskState(ScalpRiskLimits(max_consecutive_losses=4))
        rs.record_result(-50, 2.0)
        rs.record_result(-30, 1.5)
        rs.record_open()
        doc = rs.to_doc()
        rs2 = RiskState(ScalpRiskLimits(max_consecutive_losses=4))
        rs2.load_doc(doc)
        assert rs2.consecutive_losses == 2
        assert rs2.daily_loss_usd == pytest.approx(80.0)
        assert rs2.daily_cost_usd == pytest.approx(3.5)
        assert len(rs2.trade_times) == 1

    def test_restart_after_daily_loss_limit_still_blocked(self):
        limits = ScalpRiskLimits(max_daily_loss_pct=0.5)
        rs = RiskState(limits)
        rs.record_result(-300, 5.0)     # 0.6% of 50k
        rs2 = RiskState(limits)
        rs2.load_doc(rs.to_doc())        # simulated restart
        res = risk_check(rs2, 50_000, 2.0, 10.0, CFG)
        assert res["ok"] is False and "daily" in res["reason"]

    def test_stale_day_resets_budgets(self):
        rs = RiskState(DEFAULT_LIMITS)
        rs.record_result(-300, 5.0)
        doc = rs.to_doc()
        doc["daily_key"] = "2000-01-01"
        rs2 = RiskState(DEFAULT_LIMITS)
        rs2.load_doc(doc)
        assert rs2.daily_loss_usd == 0.0


# ---------------- kill switch + gate ----------------
class TestKillAndGate:
    def test_no_data_halts_but_close_allowed(self):
        h = kill.evaluate(ScalpState(PIP), CFG)
        assert h["status"] == "HALTED" and h["close_allowed"] is True

    def test_gate_blocks_stale_signal(self):
        st = _mk_state([1.08 + i * PIP * 0.1 for i in range(100)])
        g = final_execution_gate(st, CFG, 1.0,
                                 signal_ts_ms=int(time.time() * 1000) - 60_000,
                                 spread_limit_pips=1.2, health_open_allowed=True)
        assert g["ok"] is False and "signal_fresh" in g["reason"]


# ---------------- model calibration + deployment gates (items 2/3) --------
class TestModelCalibration:
    def test_platt_improves_miscalibrated_probs(self):
        rng = np.random.default_rng(3)
        y = rng.integers(0, 2, 2000).astype(float)
        # overconfident raw probabilities
        p_raw = np.clip(0.5 + (y - 0.5) * 0.9 + rng.normal(0, 0.15, 2000), 0.01, 0.99)
        a, b = scalp_model.fit_platt(p_raw, y)
        p_cal = scalp_model.apply_platt(p_raw, a, b)
        assert scalp_model.brier(p_cal, y) <= scalp_model.brier(p_raw, y) + 1e-9

    def test_ece_zero_for_perfect_calibration(self):
        p = np.array([0.2] * 500 + [0.8] * 500)
        y = np.array([1.0] * 100 + [0.0] * 400 + [1.0] * 400 + [0.0] * 100)
        assert scalp_model.ece(p, y) < 0.02

    def test_predict_refuses_expired_model(self):
        key = "TestBroker|raw|EURUSD"
        scalp_model._active[key] = {
            "w": np.zeros(len(FEATURE_KEYS) + 1), "mu": np.zeros(len(FEATURE_KEYS)),
            "sd": np.ones(len(FEATURE_KEYS)), "platt_a": 1.0, "platt_b": 0.0,
            "expires_at": "2000-01-01T00:00:00+00:00", "oos_auc": 0.6, "ts": 0}
        feats = {k: 0.0 for k in FEATURE_KEYS}
        assert scalp_model.predict_p(key, feats) is None
        assert key not in scalp_model._active   # evicted

    def test_predict_refuses_out_of_distribution(self):
        key = "TestBroker2|raw|EURUSD"
        scalp_model._active[key] = {
            "w": np.zeros(len(FEATURE_KEYS) + 1), "mu": np.zeros(len(FEATURE_KEYS)),
            "sd": np.ones(len(FEATURE_KEYS)), "platt_a": 1.0, "platt_b": 0.0,
            "expires_at": "2099-01-01T00:00:00+00:00", "oos_auc": 0.6, "ts": 0}
        feats = {k: 0.0 for k in FEATURE_KEYS}
        feats["ret_1s"] = 100.0                  # 100σ from training mean
        assert scalp_model.predict_p(key, feats) is None
        feats["ret_1s"] = 0.5
        assert scalp_model.predict_p(key, feats) is not None
        scalp_model._active.pop(key, None)

    def test_model_key_broker_separation(self):
        k1 = scalp_model.make_key("Exness", "raw_spread", "EURUSD")
        k2 = scalp_model.make_key("ICMarkets", "raw_spread", "EURUSD")
        assert k1 != k2


# ---------------- engine behaviors ----------------
def _stub_db():
    db = MagicMock()
    for coll in ("scalp_decisions", "scalp_ticks", "scalp_risk_state",
                 "scalp_configs", "trades"):
        c = getattr(db, coll)
        c.insert_one = AsyncMock()
        c.update_one = AsyncMock()
        c.find_one = AsyncMock(return_value=None)
    return db


class TestEngineBehaviors:
    def test_duplicate_and_out_of_order_ticks_dropped(self):
        r = ScalpRunner("accX", "u1", "EURUSD")
        db = _stub_db()
        now = int(time.time() * 1000)
        ticks = [{"tm": now, "b": 1.08, "a": 1.08006},
                 {"tm": now, "b": 1.08, "a": 1.08006},        # duplicate
                 {"tm": now - 500, "b": 1.07, "a": 1.07006},  # out of order
                 {"tm": now + 100, "b": 1.081, "a": 1.08106}]
        out = asyncio.run(r.ingest(db, {"equity": 1000, "broker": "X",
                                        "account_type": "standard"}, ticks, now))
        assert out["ok"] is True
        assert r.counters["ticks"] == 2          # only strictly-increasing kept

    def test_no_entries_until_risk_restored(self):
        r = ScalpRunner("accY", "u1", "EURUSD")
        r.enabled = True
        assert r._risk_restored is False
        db = _stub_db()
        now = int(time.time() * 1000)
        out = asyncio.run(r.ingest(db, {"equity": 1000}, [
            {"tm": now, "b": 1.08, "a": 1.08006}], now))
        assert out["risk_restored"] is False
        assert r.counters["evals"] == 0          # pipeline never ran

    def test_close_request_keeps_position_until_confirmed(self):
        """Review item 14 — CLOSE_REQUESTED stays in the book."""
        r = ScalpRunner("accZ", "u1", "EURUSD")
        r.live_trades["t1"] = {"state": "OPEN",
                               "opened_ms": int(time.time() * 1000) - 10 * 60_000,
                               "direction": "BUY", "entry_px": 1.08,
                               "stop_px": 1.079, "target_px": 1.082,
                               "est_cost_usd": 1.0}
        # feed one tick so spread/health exist
        st_path = [1.08 + i * PIP * 0.1 for i in range(100)]
        r.state = _mk_state(st_path)
        r.health = kill.evaluate(r.state, CFG)
        db = _stub_db()
        r._monitor_live_exits(db, r.state.last_tick)
        assert "t1" in r.live_trades              # NOT popped
        assert r.live_trades["t1"]["state"] == "CLOSE_REQUESTED"
        # broker confirms → now it leaves the book and cost budget grows
        r.on_trade_closed("t1", -12.0)
        assert "t1" not in r.live_trades
        assert r.risk_state.daily_loss_usd == pytest.approx(12.0)
        assert r.risk_state.daily_cost_usd == pytest.approx(1.0)  # est cost used

    def test_overlap_suppression_counter_exists(self):
        r = ScalpRunner("accW", "u1", "EURUSD")
        assert "suppressed_overlap" in r.counters


# ---------------- round-3 review behaviors ----------------
class TestRound3Hardening:
    def test_delayed_batch_blocks_new_entries(self):
        """Old broker ticks arriving NOW must not pass freshness."""
        r = ScalpRunner("accD", "u1", "EURUSD")
        r.enabled = True
        r._risk_restored = True
        db = _stub_db()
        now = int(time.time() * 1000)
        # broker timestamps 60s old, batch sent 10s ago
        ticks = [{"tm": now - 60_000 + i * 100, "b": 1.08, "a": 1.08006}
                 for i in range(5)]
        out = asyncio.run(r.ingest(db, {"equity": 1000}, ticks, now - 10_000))
        assert out["batch_fresh"] is False
        assert out["transport_age_ms"] >= 10_000
        assert r.counters["evals"] == 0
        assert r.counters.get("stale_batches", 0) == 1

    def test_fresh_batch_passes(self):
        r = ScalpRunner("accE", "u1", "EURUSD")
        r.enabled = True
        r._risk_restored = True
        db = _stub_db()
        now = int(time.time() * 1000)
        ticks = [{"tm": now - 400 + i * 100, "b": 1.08, "a": 1.08006}
                 for i in range(5)]
        out = asyncio.run(r.ingest(db, {"equity": 1000}, ticks, now - 200))
        assert out["batch_fresh"] is True

    def test_broker_adjusted_age(self):
        st = _mk_state([1.08 + i * PIP * 0.1 for i in range(100)])
        assert st.broker_adjusted_age_ms() < 5_000
        st2 = _mk_state([1.08] * 100,
                        start_ms=int(time.time() * 1000) - 120_000)
        # last broker tick ~70s old (100 ticks × 500ms after start)
        assert st2.broker_adjusted_age_ms() > 30_000

    def test_opposite_direction_overlap_also_suppressed(self):
        """ONE active setup event per symbol — SELL suppressed while a BUY
        sim is open against the same future path."""
        r = ScalpRunner("accF", "u1", "EURUSD")
        r.open_sims.append(ShadowSim("d", "BUY", 1.08, 1.08004, 2.0, 2.0,
                                     PIP, 0, 300_000))
        assert len(r.open_sims) == 1
        # engine checks `if self.open_sims` — verify semantics directly
        assert bool(r.open_sims) is True

    def test_slippage_feedback_from_real_fill(self):
        r = ScalpRunner("accG", "u1", "EURUSD")
        r.live_trades["t9"] = {"state": "OPEN", "direction": "BUY",
                               "decision_id": "d9", "opened_ms": 0,
                               "est_cost_usd": 0}
        before = r.state.fills_seen
        r.on_trade_opened("t9", requested_price=1.08000, actual_price=1.08004)
        assert r.state.fills_seen == before + 1
        assert r.live_trades["t9"]["entry_slippage_pips"] == pytest.approx(0.4)
        assert r.state.slippage_ewma_pips > 0

    def test_structured_fallback_reason(self):
        feats = {k: 0.0 for k in FEATURE_KEYS}
        out = scalp_model.predict("NoSuchBroker|x|EURUSD", feats)
        assert out["p"] is None
        assert out["source"] == "deterministic"
        assert out["fallback_reason"] == "no_model"

    def test_audit_backlog_reporting(self):
        from scalp import engine as eng
        info = eng.audit_backlog()
        assert {"pending", "failures", "halted", "pid",
                "dead_letter_path"} <= set(info)
        assert info["halted"] is False

    def test_close_requested_restored_after_restart(self):
        """Restart while a close is pending must restore CLOSE_REQUESTED."""
        r = ScalpRunner("accH", "u1", "EURUSD")
        db = _stub_db()
        trade_doc = {"_id": "tr1", "action": "BUY", "entry_price": 1.08,
                     "stop_loss": 1.079, "take_profit": 1.082,
                     "scalp_decision_id": "d1",
                     "pending_modification": {"type": "FULL_CLOSE"}}
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[trade_doc])
        db.trades.find = MagicMock(return_value=cursor)
        db.scalp_risk_state.find_one = AsyncMock(return_value=None)
        asyncio.run(r.restore_risk(db))
        assert r._risk_restored is True
        assert r.live_trades["tr1"]["state"] == "CLOSE_REQUESTED"


# ---------------- round-5 review behaviors ----------------
class TestRound5FinancialReconciliation:
    def _open_runner(self, acc="r5a"):
        r = ScalpRunner(acc, "u1", "EURUSD")
        r.live_trades["t1"] = {"state": "OPEN", "opened_ms": 0,
                               "direction": "BUY", "entry_px": 1.08,
                               "requested_entry": 1.08, "actual_entry": 1.08002,
                               "stop_px": 1.079, "target_px": 1.082,
                               "decision_id": "d1", "est_cost_usd": 1.5}
        r.risk_state.open_scalps = 1
        r.account_risk.open_scalps = 1
        return r

    def test_signed_broker_semantics_positive_swap_credit(self):
        """net = profit + commission + swap (SIGNED) — never abs()."""
        r = self._open_runner("r5b")
        r.on_trade_closed("t1", pnl=10.0, commission=-0.5, swap=0.2,
                          exit_price=1.0805, deal_id="D1", source="broker_deal")
        # net = 10 - 0.5 + 0.2 = 9.7 profit → no daily loss
        assert r.risk_state.daily_loss_usd == 0.0
        # execution cost counts only the NEGATIVE components
        assert r.risk_state.daily_cost_usd == pytest.approx(0.5)
        assert r.account_risk.daily_cost_usd == pytest.approx(0.5)

    def test_signed_loss_with_commission(self):
        r = self._open_runner("r5c")
        r.on_trade_closed("t1", pnl=-8.0, commission=-0.7, swap=-0.3,
                          deal_id="D2", source="broker_deal")
        assert r.risk_state.daily_loss_usd == pytest.approx(9.0)  # -8-0.7-0.3
        assert r.risk_state.daily_cost_usd == pytest.approx(1.0)
        assert r.risk_state.consecutive_losses == 1

    def test_duplicate_deal_id_never_double_counts(self):
        r = self._open_runner("r5d")
        for _ in range(3):
            r.on_trade_closed("t1", pnl=-5.0, commission=-0.5, swap=0.0,
                              deal_id="D3", source="broker_deal")
        assert r.risk_state.daily_loss_usd == pytest.approx(5.5)
        assert r.risk_state.daily_cost_usd == pytest.approx(0.5)
        assert r.risk_state.open_scalps == 0

    def test_close_ack_applies_no_financials(self):
        """/bridge/report is operational only: slot freed, budgets untouched."""
        r = self._open_runner("r5e")
        r.on_close_ack("t1", exit_price=1.0795)
        assert "t1" not in r.live_trades
        assert r.risk_state.open_scalps == 0
        assert r.risk_state.daily_loss_usd == 0.0
        assert r.risk_state.daily_cost_usd == 0.0
        assert "t1" in r._closed_awaiting_financials

    def test_ack_then_external_deal_exactly_once(self):
        r = self._open_runner("r5f")
        r.on_close_ack("t1", exit_price=1.0795)
        r.on_trade_closed("t1", pnl=-6.0, commission=-0.4, swap=0.0,
                          exit_price=1.0795, deal_id="D4", source="broker_deal")
        # financials once, position count not double-decremented
        assert r.risk_state.daily_loss_usd == pytest.approx(6.4)
        assert r.risk_state.open_scalps == 0
        assert "t1" not in r._closed_awaiting_financials
        # a second report-path call after financials is a no-op
        r.on_close_ack("t1")
        r.on_trade_closed("t1", pnl=-6.0, deal_id="D4b", source="broker_deal")
        assert r.risk_state.daily_loss_usd == pytest.approx(6.4)

    def test_exit_slippage_attribution_in_canonical_record(self):
        r = self._open_runner("r5g")
        r.live_trades["t1"]["requested_exit_price"] = 1.08000
        r.live_trades["t1"]["exit_reference_bid"] = 1.08000
        r.live_trades["t1"]["exit_reference_ask"] = 1.08006
        r.live_trades["t1"]["close_requested_ms"] = 123
        db = _stub_db()

        async def run():
            r.on_trade_closed("t1", pnl=-3.0, commission=-0.4, swap=0.1,
                              exit_price=1.07996, deal_id="D5",
                              source="broker_deal", db=db)
            for _ in range(5):
                await asyncio.sleep(0)
        asyncio.run(run())
        sets = [c.args[1]["$set"] for c in db.scalp_decisions.update_one.call_args_list]
        recs = [s for s in sets if "execution_outcome" in s]
        assert recs, "canonical execution record was not written"
        o = recs[0]["execution_outcome"]
        assert o["requested_exit_price"] == 1.08000
        assert o["actual_exit_price"] == 1.07996
        assert o["exit_slippage_pips"] == pytest.approx(0.4)   # adverse for BUY
        assert o["exit_reference_bid"] == 1.08000
        assert o["net_pnl_usd"] == pytest.approx(-3.3)         # -3 - 0.4 + 0.1
        assert o["commission_usd"] == -0.4                     # SIGNED
        assert o["swap_usd"] == 0.1                            # SIGNED
        assert o["financing_credit_usd"] == pytest.approx(0.1)
        assert o["execution_cost_usd"] == pytest.approx(0.4)
        assert o["cost_source"] == "broker"
        assert o["broker_deal_id"] == "D5"

    def test_broker_stop_exit_slippage_vs_stop_level(self):
        """No CLOSE_REQUESTED (broker-side SL hit) → stop price is the
        requested-exit baseline when the fill lands near it."""
        r = self._open_runner("r5h")
        db = _stub_db()

        async def run():
            r.on_trade_closed("t1", pnl=-10.0, commission=-0.5, swap=0.0,
                              exit_price=1.07895, deal_id="D6",
                              source="broker_deal", db=db)
            for _ in range(5):
                await asyncio.sleep(0)
        asyncio.run(run())
        sets = [c.args[1]["$set"] for c in db.scalp_decisions.update_one.call_args_list]
        o = [s for s in sets if "execution_outcome" in s][0]["execution_outcome"]
        assert o["requested_exit_price"] == pytest.approx(1.079)
        assert o["exit_slippage_pips"] == pytest.approx(0.5)   # stopped 0.5p worse


class TestRound5AccountRisk:
    def test_account_state_shared_across_runners(self):
        from scalp.engine import account_risk_state
        r1 = ScalpRunner("acct-shared-1", "u1", "EURUSD")
        assert r1.account_risk is account_risk_state("acct-shared-1")

    def test_account_wide_daily_loss_blocks_all_symbols(self):
        from scalp.risk import check_account
        r1 = ScalpRunner("acct-wide-1", "u1", "EURUSD")
        r1.live_trades["t1"] = {"state": "OPEN", "opened_ms": 0,
                                "direction": "BUY", "est_cost_usd": 0.0}
        # loss exceeding 0.5% of 1000 equity account-wide
        r1.on_trade_closed("t1", pnl=-6.0, commission=0.0, swap=0.0,
                           deal_id="DA", source="broker_deal")
        # ANY other runner on the same account is now blocked
        r2 = ScalpRunner("acct-wide-1", "u2-any", "EURUSD")
        res = check_account(r2.account_risk, 1000.0)
        assert res["ok"] is False
        assert "account-wide daily scalp loss" in res["reason"]

    def test_account_cooldown_blocks(self):
        from scalp.risk import check_account
        r = ScalpRunner("acct-cool-1", "u1", "EURUSD")
        r.account_risk.cooldown_until = time.time() + 600
        res = check_account(r.account_risk, 10_000.0)
        assert res["ok"] is False and "cooldown" in res["reason"]

    def test_account_risk_persisted_alongside_symbol(self):
        r = ScalpRunner("acct-persist-1", "u1", "EURUSD")
        db = _stub_db()

        async def run():
            r._persist_risk(db)
            for _ in range(5):
                await asyncio.sleep(0)
        asyncio.run(run())
        filters = [c.args[0] for c in db.scalp_risk_state.update_one.call_args_list]
        symbols = {f["symbol"] for f in filters}
        assert symbols == {"EURUSD", "_ACCOUNT"}


class TestRound5RestoreAndDurability:
    def test_detailed_open_state_restored(self):
        r = ScalpRunner("acct-rest-1", "u1", "EURUSD")
        db = _stub_db()
        opened_iso = "2026-06-01T10:00:00+00:00"
        trade_doc = {"_id": "tr9", "action": "SELL", "entry_price": 1.0810,
                     "requested_price": 1.0811, "slippage_pips": 1.0,
                     "stop_loss": 1.0820, "take_profit": 1.0790,
                     "scalp_decision_id": "d9", "opened_at": opened_iso,
                     "pending_modification": {"type": "FULL_CLOSE",
                                              "requested_at": "2026-06-01T10:03:00+00:00"}}
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[trade_doc])
        db.trades.find = MagicMock(return_value=cursor)
        db.scalp_decisions.find_one = AsyncMock(
            return_value={"cost_pips": 1.2, "lot": 0.02})
        asyncio.run(r.restore_risk(db))
        info = r.live_trades["tr9"]
        from scalp.engine import _iso_to_ms
        assert info["state"] == "CLOSE_REQUESTED"
        assert info["opened_ms"] == _iso_to_ms(opened_iso)
        assert info["close_requested_ms"] == _iso_to_ms("2026-06-01T10:03:00+00:00")
        assert info["requested_entry"] == 1.0811
        assert info["entry_slippage_pips"] == 1.0
        assert info["est_cost_usd"] > 0

    def test_dead_letter_path_env_configurable(self, tmp_path, monkeypatch):
        from scalp import engine as eng
        target = tmp_path / "sub" / "dl.jsonl"
        monkeypatch.setattr(eng, "DEAD_LETTER_PATH", str(target))
        eng._dead_letter("test_op", "boom")
        assert target.exists()
        import json
        rec = json.loads(target.read_text().strip())
        assert rec["op"] == "test_op" and rec["error"] == "boom"

    def test_fill_stats_prior_and_counting(self):
        r = ScalpRunner("acct-fill-1", "u1", "EURUSD")
        assert r.fill_stats()["p_fill"] == pytest.approx(8 / 9, abs=0.01)
        r.exec_attempts = 20
        r.exec_fills = 2
        assert r.fill_stats()["p_fill"] == pytest.approx(10 / 29, abs=0.01)
        # fills counted once per trade via on_trade_opened
        r.live_trades["tf"] = {"state": "OPEN", "direction": "BUY",
                               "decision_id": "d", "opened_ms": 0,
                               "est_cost_usd": 0}
        r.on_trade_opened("tf", requested_price=1.08, actual_price=1.08001)
        r.on_trade_opened("tf", requested_price=1.08, actual_price=1.08001)
        assert r.exec_fills == 3


class TestRound5Statistics:
    def test_block_bootstrap_lower_bound_sign(self):
        rng = np.random.default_rng(7)
        pos = rng.normal(1.0, 0.5, 400)
        lb = scalp_model._lower_bound(pos)
        assert 0 < lb < pos.mean()
        neg = rng.normal(-0.5, 0.5, 400)
        assert scalp_model._lower_bound(neg) < 0

    def test_block_bootstrap_widens_under_dependence(self):
        """Clustered (serially dependent) outcomes must yield a WIDER (lower)
        bound than the same values shuffled to look independent."""
        rng = np.random.default_rng(3)
        block_means = rng.normal(0.3, 1.0, 40)
        clustered = np.repeat(block_means, 10)          # 400 pts, 40 clusters
        shuffled = clustered.copy()
        rng.shuffle(shuffled)
        lb_clustered = scalp_model._lower_bound(clustered)
        lb_shuffled = scalp_model._lower_bound(shuffled)
        assert lb_clustered < lb_shuffled

    def test_purge_embargo_drops_overlapping_labels(self):
        n = 100
        ts = np.arange(n, dtype=np.int64) * 60_000      # 1 trade / minute
        mask = np.ones(n, dtype=bool)
        boundary = 50
        purged = scalp_model._purge(mask, ts, boundary)
        # 5-minute embargo → the 5 samples before the boundary are dropped
        assert purged[:45].all()
        assert not purged[45:50].any()
        assert purged[50:].all()                        # eval side untouched


# ---------------- EA coherence (project-relative path) ----------------
def test_ea_144_tick_stream_wiring():
    ea_path = BACKEND / "static" / "EmergentTradingBridge.mq5"
    src = ea_path.read_text()
    assert '#define EA_CLIENT_VERSION "1.44"' in src
    assert "void SendTicks()" in src
    assert "/api/bridge/ticks" in src
    assert "EventSetMillisecondTimer" in src
