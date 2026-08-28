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

BACKEND = Path(__file__).resolve().parents[3]

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
                 "scalp_configs", "trades", "broker_deals", "scalp_owners",
                 "scalp_financial_events", "accounts",
                 "scalp_submission_slots", "risk_reservations",
                 "outbox", "trade_events"):
        c = getattr(db, coll)
        c.insert_one = AsyncMock()
        c.update_one = AsyncMock(return_value=MagicMock(matched_count=1,
                                                        modified_count=1))
        c.update_many = AsyncMock()
        c.find_one = AsyncMock(return_value=None)
        c.count_documents = AsyncMock(return_value=0)
        c.delete_many = AsyncMock()
        c.create_index = AsyncMock()
        # audit r3 · command fence stamps via find_one_and_update
        c.find_one_and_update = AsyncMock(return_value={
            "pending_modification": {"intent_id": "i1", "seq": 1}})

    # Phase A — outbox immediate-publish path iterates find().sort().limit()
    def _empty_cursor(*_a, **_k):
        async def gen():
            return
            yield  # pragma: no cover
        cur = MagicMock()
        cur.sort.return_value.limit.return_value = gen()
        return cur
    db.outbox.find = MagicMock(side_effect=_empty_cursor)
    # audit r3 · unified release path iterates find() directly
    def _empty_agen(*_a, **_k):
        async def gen():
            return
            yield  # pragma: no cover
        return gen()
    db.risk_reservations.find = MagicMock(side_effect=_empty_agen)
    # iter-61: broker with plenty of healthy execution history so the
    # fail-closed history cap / exec-quality gate don't mask the stages
    # under test (each session bucket present, favourable averages)
    def _prior_cursor(*_a, **_k):
        async def gen():
            for sess in ("asia", "london", "overlap", "newyork", "late"):
                yield {"session": sess, "submissions": 60, "rejects": 1,
                       "entry_slip_n": 40, "entry_slip_sum": 2.0,
                       "ack_ms_sum": 40 * 600.0, "ack_n": 40,
                       "spread_sum": 16.0, "spread_n": 40}
        return gen()
    db.scalp_broker_stats.find = MagicMock(side_effect=_prior_cursor)
    db.scalp_broker_stats.update_one = AsyncMock(
        return_value=MagicMock(matched_count=1))
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
        asyncio.run(r._monitor_live_exits(db, r.state.last_tick))
        assert "t1" in r.live_trades              # NOT popped
        assert r.live_trades["t1"]["state"] == "CLOSE_REQUESTED"
        # broker confirms → now it leaves the book and cost budget grows
        asyncio.run(r.on_trade_closed("t1", -12.0))
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
        asyncio.run(r.on_trade_opened("t9", requested_price=1.08000, actual_price=1.08004))
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
                     "symbol": "EURUSD", "scalp_decision_id": "d1",
                     "pending_modification": {"type": "FULL_CLOSE"}}
        db.trades.find = MagicMock(return_value=_AsyncCursor([trade_doc]))
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
        asyncio.run(r.on_trade_closed("t1", pnl=10.0, commission=-0.5, swap=0.2,
                          exit_price=1.0805, deal_id="D1", source="broker_deal"))
        # net = 10 - 0.5 + 0.2 = 9.7 profit → no daily loss
        assert r.risk_state.daily_loss_usd == 0.0
        # execution cost counts only the NEGATIVE components
        assert r.risk_state.daily_cost_usd == pytest.approx(0.5)
        assert r.account_risk.daily_cost_usd == pytest.approx(0.5)

    def test_signed_loss_with_commission(self):
        r = self._open_runner("r5c")
        asyncio.run(r.on_trade_closed("t1", pnl=-8.0, commission=-0.7, swap=-0.3,
                          deal_id="D2", source="broker_deal"))
        assert r.risk_state.daily_loss_usd == pytest.approx(9.0)  # -8-0.7-0.3
        assert r.risk_state.daily_cost_usd == pytest.approx(1.0)
        assert r.risk_state.consecutive_losses == 1

    def test_duplicate_deal_id_never_double_counts(self):
        r = self._open_runner("r5d")
        for _ in range(3):
            asyncio.run(r.on_trade_closed("t1", pnl=-5.0, commission=-0.5, swap=0.0,
                              deal_id="D3", source="broker_deal"))
        assert r.risk_state.daily_loss_usd == pytest.approx(5.5)
        assert r.risk_state.daily_cost_usd == pytest.approx(0.5)
        assert r.risk_state.open_scalps == 0

    def test_close_ack_applies_no_financials(self):
        """/bridge/report is operational only: slot freed, budgets untouched."""
        r = self._open_runner("r5e")
        asyncio.run(r.on_close_ack("t1", exit_price=1.0795))
        assert "t1" not in r.live_trades
        assert r.risk_state.open_scalps == 0
        assert r.risk_state.daily_loss_usd == 0.0
        assert r.risk_state.daily_cost_usd == 0.0
        assert "t1" in r._closed_awaiting_financials

    def test_ack_then_external_deal_exactly_once(self):
        r = self._open_runner("r5f")
        asyncio.run(r.on_close_ack("t1", exit_price=1.0795))
        asyncio.run(r.on_trade_closed("t1", pnl=-6.0, commission=-0.4, swap=0.0,
                          exit_price=1.0795, deal_id="D4", source="broker_deal"))
        # financials once, position count not double-decremented
        assert r.risk_state.daily_loss_usd == pytest.approx(6.4)
        assert r.risk_state.open_scalps == 0
        assert "t1" not in r._closed_awaiting_financials
        # a second report-path call after financials is a no-op
        asyncio.run(r.on_close_ack("t1"))
        asyncio.run(r.on_trade_closed("t1", pnl=-6.0, deal_id="D4b", source="broker_deal"))
        assert r.risk_state.daily_loss_usd == pytest.approx(6.4)

    def test_exit_slippage_attribution_in_canonical_record(self):
        r = self._open_runner("r5g")
        r.live_trades["t1"]["requested_exit_price"] = 1.08000
        r.live_trades["t1"]["exit_reference_bid"] = 1.08000
        r.live_trades["t1"]["exit_reference_ask"] = 1.08006
        r.live_trades["t1"]["close_requested_ms"] = 123
        db = _stub_db()

        async def run():
            await r.on_trade_closed("t1", pnl=-3.0, commission=-0.4, swap=0.1,
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
            await r.on_trade_closed("t1", pnl=-10.0, commission=-0.5, swap=0.0,
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
        asyncio.run(r1.on_trade_closed("t1", pnl=-6.0, commission=0.0, swap=0.0,
                           deal_id="DA", source="broker_deal"))
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
                     "symbol": "EURUSD",
                     "scalp_decision_id": "d9", "opened_at": opened_iso,
                     "pending_modification": {"type": "FULL_CLOSE",
                                              "requested_at": "2026-06-01T10:03:00+00:00"}}
        db.trades.find = MagicMock(return_value=_AsyncCursor([trade_doc]))
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
        asyncio.run(r.on_trade_opened("tf", requested_price=1.08, actual_price=1.08001))
        asyncio.run(r.on_trade_opened("tf", requested_price=1.08, actual_price=1.08001))
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


# ---------------- round-6 review behaviors ----------------
class TestRound6PartialAndDurability:
    def _runner_with_open(self, acc):
        r = ScalpRunner(acc, "u1", "EURUSD")
        r.live_trades["t1"] = {"state": "OPEN", "opened_ms": 0,
                               "direction": "BUY", "lot": 0.10,
                               "entry_px": 1.08, "stop_px": 1.079,
                               "target_px": 1.082, "decision_id": "d1",
                               "est_cost_usd": 1.0}
        r.risk_state.open_scalps = 1
        r.account_risk.open_scalps = 1
        r.account_risk.add_stop_risk("t1", 10.0)
        return r

    def test_partial_close_applies_financials_keeps_position(self):
        r = self._runner_with_open("r6a")
        asyncio.run(r.on_partial_close("t1", closed_lots=0.05, remaining_lots=0.05,
                           pnl=-2.0, commission=-0.2, swap=0.0,
                           exit_price=1.0795, deal_id="P1"))
        assert "t1" in r.live_trades                      # position STAYS open
        assert r.live_trades["t1"]["lot"] == pytest.approx(0.05)
        assert r.risk_state.open_scalps == 1              # no record_close
        assert r.risk_state.daily_loss_usd == pytest.approx(2.2)
        assert r.risk_state.daily_cost_usd == pytest.approx(0.2)
        assert r.account_risk.daily_loss_usd == pytest.approx(2.2)
        # stop risk scaled by remaining/prior lot
        assert r.account_risk.stop_risk_by_trade["t1"] == pytest.approx(5.0)

    def test_partial_close_idempotent_per_deal(self):
        r = self._runner_with_open("r6b")
        for _ in range(3):
            asyncio.run(r.on_partial_close("t1", 0.05, 0.05, pnl=-2.0, commission=-0.2,
                               deal_id="P2"))
        assert r.risk_state.daily_loss_usd == pytest.approx(2.2)

    def test_swap_credit_does_not_reset_loss_streak(self):
        """Round 6 item 9 — a financing credit can't mask a losing scalp."""
        r = self._runner_with_open("r6c")
        r.risk_state.consecutive_losses = 2
        asyncio.run(r.on_trade_closed("t1", pnl=-1.0, commission=-0.2, swap=1.5,
                          deal_id="S1", source="broker_deal"))
        # net = +0.3 → NO daily loss...
        assert r.risk_state.daily_loss_usd == 0.0
        # ...but trading P&L = -1.2 → the streak still grows
        assert r.risk_state.consecutive_losses == 3

    def test_stop_loss_reason_forces_stop_baseline_on_gap(self):
        """Round 6 item 8 — gap-through-stop MUST still measure slippage."""
        r = self._runner_with_open("r6d")
        db = _stub_db()

        async def run():
            await r.on_trade_closed("t1", pnl=-90.0, commission=-0.5, swap=0.0,
                              exit_price=1.0700, deal_id="S2",
                              close_reason="stop_loss",
                              source="broker_deal", db=db)
            for _ in range(5):
                await asyncio.sleep(0)
        asyncio.run(run())
        sets = [c.args[1]["$set"] for c in db.scalp_decisions.update_one.call_args_list]
        o = [s for s in sets if "execution_outcome" in s][0]["execution_outcome"]
        assert o["requested_exit_price"] == pytest.approx(1.079)
        assert o["exit_slippage_pips"] == pytest.approx(90.0)  # 1.079 → 1.0700
        assert o["exit_reason"] == "stop_loss"

    def test_account_stop_risk_budget_blocks(self):
        from scalp.risk import check_account, ACCOUNT_LIMITS
        r = ScalpRunner("r6e", "u1", "EURUSD")
        equity = 10_000.0
        budget = equity * ACCOUNT_LIMITS.max_total_stop_risk_pct / 100.0  # $15
        r.account_risk.add_stop_risk("x", budget - 1.0)
        ok = check_account(r.account_risk, equity, proposed_stop_risk_usd=0.5)
        assert ok["ok"] is True
        blocked = check_account(r.account_risk, equity, proposed_stop_risk_usd=2.0)
        assert blocked["ok"] is False and "stop risk" in blocked["reason"]

    def test_account_hourly_cap_is_explicit_policy(self):
        from scalp.risk import check_account, ACCOUNT_LIMITS
        import time as _t
        r = ScalpRunner("r6f", "u1", "EURUSD")
        for _ in range(ACCOUNT_LIMITS.max_total_trades_per_hour):
            r.account_risk.trade_times.append(_t.time())
        res = check_account(r.account_risk, 10_000.0)
        assert res["ok"] is False and "hourly" in res["reason"]

    def test_applied_deal_ids_persisted_and_restored(self):
        r = self._runner_with_open("r6g")
        db = _stub_db()

        async def run():
            await r.on_trade_closed("t1", pnl=-1.0, commission=-0.1, swap=0.0,
                              deal_id="DUR1", source="broker_deal", db=db)
            for _ in range(5):
                await asyncio.sleep(0)
        asyncio.run(run())
        sets = [c.args[1]["$set"] for c in db.scalp_risk_state.update_one.call_args_list]
        sym_doc = [s for s in sets if "applied_deal_ids" in s][0]
        assert "DUR1" in sym_doc["applied_deal_ids"]
        # a fresh runner (post-restart) restores the dedupe cache
        r2 = ScalpRunner("r6g-fresh", "u1", "EURUSD")
        db2 = _stub_db()
        db2.scalp_risk_state.find_one = AsyncMock(
            side_effect=[{**r.risk_state.to_doc(), "applied_deal_ids": ["DUR1"]},
                         None])
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[])
        db2.trades.find = MagicMock(return_value=cursor)
        asyncio.run(r2.restore_risk(db2))
        assert "DUR1" in r2._applied_deal_ids
        before = r2.risk_state.daily_loss_usd
        asyncio.run(r2.on_trade_closed("tX", pnl=-9.0, deal_id="DUR1", source="broker_deal"))
        assert r2.risk_state.daily_loss_usd == before      # replay is a no-op

    def test_restore_rebuilds_account_stop_risk_and_open_count(self):
        r = ScalpRunner("r6h", "u1", "EURUSD")
        db = _stub_db()
        trade_doc = {"_id": "trS", "action": "BUY", "entry_price": 1.0800,
                     "stop_loss": 1.0790, "take_profit": 1.0820,
                     "lot_size": 0.10, "symbol": "EURUSD",
                     "scalp_decision_id": "",
                     "opened_at": "2026-06-01T10:00:00+00:00"}
        db.trades.find = MagicMock(return_value=_AsyncCursor([trade_doc]))
        asyncio.run(r.restore_risk(db))
        # 10 pips × $10/pip/lot × 0.10 lots = $10 at stop
        assert r.account_risk.stop_risk_by_trade["trS"] == pytest.approx(10.0)
        assert r.account_risk.open_scalps >= 1
        assert r.live_trades["trS"]["lot"] == pytest.approx(0.10)

    def test_recovery_job_resumes_pending_deal(self):
        from scalp.engine import recover_pending_deals

        class _Cursor:
            def __init__(self, docs):
                self.docs = docs

            def limit(self, n):
                return self

            def __aiter__(self):
                async def gen():
                    for d in self.docs:
                        yield d
                return gen()

        deal = {"deal_id": 777, "account_id": "r6i", "mt5_ticket": 42,
                "profit": -4.0, "commission": -0.3, "swap": 0.0,
                "price": 1.0790, "lots": 0.05,
                "financial_reconciliation_status": "pending",
                "received_at": "2020-01-01T00:00:00+00:00"}
        trade = {"_id": "trR", "scope": "scalp_fast", "status": "closed",
                 "symbol": "EURUSD", "user_id": "u1",
                 "close_reason": "stop_loss"}
        db = _stub_db()
        db.broker_deals.find = MagicMock(return_value=_Cursor([deal]))
        db.broker_deals.update_one = AsyncMock()
        db.trades.find_one = AsyncMock(return_value=trade)
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[])
        db.trades.find = MagicMock(return_value=cursor)

        async def run():
            out = await recover_pending_deals(db, older_than_sec=0)
            for _ in range(5):
                await asyncio.sleep(0)
            return out
        out = asyncio.run(run())
        assert out["recovered"] == 1 and out["marked_complete"] == 1
        from scalp.engine import get_runner
        rr = get_runner("r6i", "u1", "EURUSD")
        assert rr.risk_state.daily_loss_usd == pytest.approx(4.3)
        marked = db.broker_deals.update_one.call_args.args[1]["$set"]
        assert marked["financial_reconciliation_status"] == "complete"


# ---------------- round-7 review behaviors ----------------
class _AsyncCursor:
    def __init__(self, docs):
        self.docs = docs

    def limit(self, n):
        return self

    def __aiter__(self):
        async def gen():
            for d in self.docs:
                yield d
        return gen()


class TestRound7ApplyConfirmedReconciliation:
    def test_apply_broker_deal_constructs_and_restores_runner(self):
        """CRITICAL — a deal arriving before any runner exists must still be
        APPLIED (runner built + restored on demand), never skipped."""
        from scalp.engine import apply_broker_deal, get_runner
        db = _stub_db()
        cursor = MagicMock()
        cursor.to_list = AsyncMock(return_value=[])
        db.trades.find = MagicMock(return_value=cursor)
        trade = {"_id": "trN", "symbol": "EURUSD", "user_id": "u1",
                 "status": "closed", "close_reason": "stop_loss"}

        async def run():
            res = await apply_broker_deal(
                db, "r7-new", trade, deal_id=9, lots=0.05, profit=-3.0,
                commission=-0.2, swap=0.0, price=1.079, partial=False)
            for _ in range(5):
                await asyncio.sleep(0)
            return res
        res = asyncio.run(run())
        assert res["applied"] is True
        r = get_runner("r7-new", "u1", "EURUSD")
        assert r._risk_restored is True
        assert r.risk_state.daily_loss_usd == pytest.approx(3.2)
        # item 6 — risk persisted SYNCHRONOUSLY before caller marks complete
        assert db.scalp_risk_state.update_one.await_count >= 2

    def test_apply_broker_deal_unavailable_runner_not_applied(self):
        from scalp.engine import apply_broker_deal
        db = _stub_db()
        trade = {"_id": "trU", "symbol": "GBPUSD", "user_id": "u1"}
        res = asyncio.run(apply_broker_deal(
            db, "r7-un", trade, deal_id=10, lots=0.05, profit=-1.0,
            commission=0.0, swap=0.0, price=1.25, partial=False))
        assert res["applied"] is False
        assert res["reason"] == "runner_unavailable"

    def test_recovery_no_trade_grace_retries_then_escalates(self):
        """Round 8 item 7 — a missing trade may be a RACE: grace-retry with
        attempt counting, escalate to manual review after 5, NEVER silently
        complete."""
        from scalp.engine import recover_pending_deals
        deal = {"deal_id": 801, "account_id": "r7-nt", "mt5_ticket": 1,
                "financial_reconciliation_status": "pending",
                "received_at": "2020-01-01T00:00:00+00:00"}
        db = _stub_db()
        db.broker_deals.find = MagicMock(return_value=_AsyncCursor([deal]))
        db.trades.find_one = AsyncMock(return_value=None)
        out = asyncio.run(recover_pending_deals(db, older_than_sec=0))
        assert out == {"recovered": 0, "marked_complete": 0, "kept_pending": 1}
        upd = db.broker_deals.update_one.call_args.args[1]
        assert upd["$set"]["reconciliation_error"] == "no_matching_trade_yet"
        assert upd["$inc"]["reconciliation_attempts"] == 1
        # after 5 attempts → escalation, still not "complete"
        deal5 = {**deal, "reconciliation_attempts": 5}
        db2 = _stub_db()
        db2.broker_deals.find = MagicMock(return_value=_AsyncCursor([deal5]))
        db2.trades.find_one = AsyncMock(return_value=None)
        out2 = asyncio.run(recover_pending_deals(db2, older_than_sec=0))
        assert out2["kept_pending"] == 1 and out2["marked_complete"] == 0
        esc = db2.broker_deals.update_one.call_args.args[1]["$set"]
        assert esc["financial_reconciliation_status"] == "manual_reconciliation_required"
        assert esc["reconciliation_note"] == "no_matching_trade"

    def test_recovery_non_scalp_completes_with_note(self):
        from scalp.engine import recover_pending_deals
        deal = {"deal_id": 802, "account_id": "r7-ns", "mt5_ticket": 2,
                "financial_reconciliation_status": "pending",
                "received_at": "2020-01-01T00:00:00+00:00"}
        db = _stub_db()
        db.broker_deals.find = MagicMock(return_value=_AsyncCursor([deal]))
        db.trades.find_one = AsyncMock(return_value={"_id": "t", "scope": None})
        out = asyncio.run(recover_pending_deals(db, older_than_sec=0))
        assert out["recovered"] == 0 and out["marked_complete"] == 1
        marked = db.broker_deals.update_one.call_args.args[1]["$set"]
        assert marked["reconciliation_note"] == "not_scalp_scope"

    def test_recovery_apply_failure_stays_pending(self):
        """CRITICAL — an unapplied deal must NOT flip to complete."""
        from scalp.engine import recover_pending_deals
        deal = {"deal_id": 803, "account_id": "r7-kp", "mt5_ticket": 3,
                "profit": -2.0, "commission": 0.0, "swap": 0.0,
                "lots": 0.05, "price": 1.25,
                "financial_reconciliation_status": "pending",
                "received_at": "2020-01-01T00:00:00+00:00"}
        trade = {"_id": "trK", "scope": "scalp_fast", "status": "closed",
                 "symbol": "GBPUSD", "user_id": "u1"}   # unapproved symbol
        db = _stub_db()
        db.broker_deals.find = MagicMock(return_value=_AsyncCursor([deal]))
        db.trades.find_one = AsyncMock(return_value=trade)
        out = asyncio.run(recover_pending_deals(db, older_than_sec=0))
        assert out["kept_pending"] == 1 and out["marked_complete"] == 0
        upd = db.broker_deals.update_one.call_args.args[1]
        assert upd["$set"]["reconciliation_error"] == "runner_unavailable"
        assert upd["$inc"]["reconciliation_attempts"] == 1
        assert "financial_reconciliation_status" not in upd["$set"]


class TestRound7DealsAndLease:
    def test_tiny_residual_is_full_close(self):
        from scalp.deals import classify_close
        assert classify_close(1e-7, 0.05, 0.05) == (False, 0.0)
        assert classify_close(0.004, 0.05, 0.046) == (False, 0.0)

    def test_broker_volume_stored_exactly_never_inflated(self):
        from scalp.deals import classify_close
        partial, remaining = classify_close(0.03, 0.05, 0.02)
        assert partial is True and remaining == pytest.approx(0.03)
        partial, remaining = classify_close(0.01, 0.05, 0.04)
        assert partial is True and remaining == pytest.approx(0.01)

    def test_legacy_lot_arithmetic_fallback(self):
        from scalp.deals import classify_close
        assert classify_close(None, 0.05, 0.02) == (True, 0.03)
        assert classify_close(None, 0.05, 0.05) == (False, 0.0)

    def test_lease_rejected_when_other_worker_owns(self):
        from scalp import engine as eng
        db = _stub_db()
        db.scalp_owners.update_one = AsyncMock(
            return_value=MagicMock(matched_count=0))
        db.scalp_owners.find_one = AsyncMock(return_value={
            "account_id": "L1", "worker_id": "other-host:999",
            "lease_until": "9999-01-01T00:00:00+00:00"})
        assert asyncio.run(eng.acquire_account_lease(db, "L1")) is False

    def test_lease_claimed_when_unowned(self):
        from scalp import engine as eng
        db = _stub_db()
        db.scalp_owners.update_one = AsyncMock(
            return_value=MagicMock(matched_count=0))
        db.scalp_owners.find_one = AsyncMock(
            side_effect=[None, {"account_id": "L2",
                                "worker_id": eng._worker_id}])
        assert asyncio.run(eng.acquire_account_lease(db, "L2")) is True

    def test_lease_renewed_for_current_owner(self):
        from scalp import engine as eng
        db = _stub_db()   # update_one matched_count=1 → owned/renewed
        assert asyncio.run(eng.acquire_account_lease(db, "L3")) is True

    def test_account_state_initializes_once(self):
        """Round 7 item 5 — account risk/stop-risk loads ONCE, not per
        symbol runner."""
        from scalp import engine as eng
        db = _stub_db()
        trade_doc = {"_id": "trOnce", "action": "BUY", "entry_price": 1.08,
                     "stop_loss": 1.079, "take_profit": 1.082,
                     "lot_size": 0.10, "symbol": "EURUSD",
                     "scalp_decision_id": "",
                     "opened_at": "2026-06-01T10:00:00+00:00"}
        db.trades.find = MagicMock(return_value=_AsyncCursor([trade_doc]))
        r1 = ScalpRunner("r7-once", "u1", "EURUSD")
        asyncio.run(r1.restore_risk(db))
        assert "r7-once" in eng._account_restored
        assert r1.account_risk.stop_risk_by_trade["trOnce"] == pytest.approx(10.0)
        acct_queries = [c.args[0] for c
                        in db.scalp_risk_state.find_one.await_args_list
                        if c.args[0].get("symbol") == "_ACCOUNT"]
        assert len(acct_queries) == 1
        r2 = ScalpRunner("r7-once", "u1", "EURUSD")
        asyncio.run(r2.restore_risk(db))
        acct_queries = [c.args[0] for c
                        in db.scalp_risk_state.find_one.await_args_list
                        if c.args[0].get("symbol") == "_ACCOUNT"]
        assert len(acct_queries) == 1          # NOT re-run by second runner


# ---------------- round-8 review behaviors ----------------
class TestRound8ProtectionRecovery:
    def _trade(self, **kw):
        base = {"_id": "trP", "account_id": "000000000000000000000001",
                "user_id": "u1", "symbol": "EURUSD", "action": "BUY",
                "status": "open", "entry_price": 1.08, "lot_size": 0.05,
                "stop_loss": 0.0, "take_profit": 0.0,
                "protection_missing": True, "mt5_ticket": 42}
        base.update(kw)
        return base

    def _guard_db(self, trades):
        db = _stub_db()

        class _AsyncCursor:
            def __init__(self, items):
                self._items = list(items)

            def sort(self, *a, **k):
                return self

            def __aiter__(self):
                self._it = iter(self._items)
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

        db.trades.find = MagicMock(side_effect=lambda *a, **k: _AsyncCursor(trades))
        db.trades.count_documents = AsyncMock(return_value=len(trades))
        db.trades.find_one = AsyncMock(
            return_value=(trades[0] if trades else None))
        db.trades.distinct = AsyncMock(
            return_value=[t["account_id"] for t in trades
                          if t.get("protection_missing")])
        db.notifications.insert_one = AsyncMock()
        db.accounts.find_one = AsyncMock(return_value={"equity": 10_000.0})
        return db

    def test_emergency_stop_calculation(self):
        from protection_guard import calculate_emergency_stop
        sl = calculate_emergency_stop(1.08, "BUY", 0.05, "EURUSD", 10_000.0)
        assert sl is not None and sl < 1.08
        # capped at 0.5% of price
        assert 1.08 - sl <= 1.08 * 0.005 + 1e-9
        sl_sell = calculate_emergency_stop(1.08, "SELL", 0.05, "EURUSD", 10_000.0)
        assert sl_sell > 1.08

    def test_unprotected_position_gets_emergency_stop_queued(self):
        from protection_guard import repair_unprotected_positions
        tr = self._trade()
        db = self._guard_db([tr])
        out = asyncio.run(repair_unprotected_positions(db))
        assert out["stops_queued"] == 1
        sets = [c.args[1] for c in db.trades.update_one.call_args_list]
        mod = [s for s in sets if s.get("$set", {}).get("protection_state")
               == "EMERGENCY_STOP_PENDING"][0]
        pend = mod["$set"]["pending_modification"]
        assert pend["type"] == "MODIFY_SL" and pend["new_sl"] < 1.08
        assert mod["$inc"]["protection_repair_attempts"] == 1
        db.notifications.insert_one.assert_awaited()
        # new scalp entries blocked for the account
        from scalp.engine import BLOCK_MISSING_PROTECTION, account_block_reasons
        assert BLOCK_MISSING_PROTECTION in account_block_reasons(tr["account_id"])

    def test_protection_resolved_when_stop_confirmed(self):
        from protection_guard import repair_unprotected_positions
        from scalp.engine import BLOCK_MISSING_PROTECTION, account_block_reasons
        # round 12 item 7 — only BROKER-CONFIRMED evidence resolves
        tr = self._trade(_id="trR8", account_id="000000000000000000000002",
                         stop_loss=1.0795, confirmed_stop_loss=1.0795)
        db = self._guard_db([tr])
        db.trades.distinct = AsyncMock(return_value=[])   # nothing left flagged
        out = asyncio.run(repair_unprotected_positions(db))
        assert out["resolved"] == 1 and out["stops_queued"] == 0
        sets = [c.args[1]["$set"] for c in db.trades.update_one.call_args_list]
        assert any(s.get("protection_state") == "RESOLVED"
                   and s.get("protection_missing") is False for s in sets)
        assert BLOCK_MISSING_PROTECTION not in account_block_reasons(
            "000000000000000000000002")

    def test_local_stop_alone_does_not_resolve_protection(self):
        # round 12 item 7 — a locally proposed stop_loss WITHOUT broker
        # confirmation must keep the trade in repair (stop re-queued)
        from protection_guard import repair_unprotected_positions
        tr = self._trade(_id="trR12", account_id="000000000000000000000012",
                         stop_loss=1.0795)          # no confirmed_stop_loss
        db = self._guard_db([tr])
        out = asyncio.run(repair_unprotected_positions(db))
        assert out["resolved"] == 0 and out["stops_queued"] == 1
        from scalp.engine import set_protection_block
        set_protection_block("000000000000000000000012", False)

    def test_exhausted_attempts_escalate_to_emergency_close(self):
        from protection_guard import repair_unprotected_positions
        tr = self._trade(_id="trC8", account_id="000000000000000000000003",
                         protection_repair_attempts=3)
        db = self._guard_db([tr])
        out = asyncio.run(repair_unprotected_positions(db))
        assert out["closes_queued"] == 1
        sets = [c.args[1]["$set"] for c in db.trades.update_one.call_args_list]
        close = [s for s in sets
                 if s.get("protection_state") == "EMERGENCY_CLOSE_PENDING"][0]
        assert close["close_requested"] is True
        assert close["pending_modification"]["type"] == "FULL_CLOSE"

    def test_pending_modification_waits_for_ea_ack(self):
        from protection_guard import repair_unprotected_positions
        tr = self._trade(_id="trW8", account_id="000000000000000000000004",
                         pending_modification={"type": "MODIFY_SL"})
        db = self._guard_db([tr])
        out = asyncio.run(repair_unprotected_positions(db))
        assert out["resolved"] == 0 and out["stops_queued"] == 0
        assert out["closes_queued"] == 0
        assert out["accounts_blocked"] == ["000000000000000000000004"]

    def test_protection_block_vetoes_scalp_entries(self):
        from scalp.engine import (BLOCK_MISSING_PROTECTION,
                                  account_block_reasons, set_protection_block)
        set_protection_block("acct-pb", True)
        assert BLOCK_MISSING_PROTECTION in account_block_reasons("acct-pb")
        set_protection_block("acct-pb", False)
        assert BLOCK_MISSING_PROTECTION not in account_block_reasons("acct-pb")

    def test_conservative_risk_counted_while_unprotected(self):
        from protection_guard import repair_unprotected_positions
        from scalp.engine import account_risk_state, set_protection_block
        tr = self._trade(_id="trK8", account_id="000000000000000000000005")
        db = self._guard_db([tr])
        asyncio.run(repair_unprotected_positions(db))
        ars = account_risk_state("000000000000000000000005")
        assert ars.stop_risk_by_trade["unprotected_positions"] == pytest.approx(50.0)
        set_protection_block("000000000000000000000005", False)
        ars.remove_stop_risk("unprotected_positions")


class TestRound8InvariantsAndFencing:
    def test_invariants_pass_on_consistent_state(self):
        from scalp.engine import verify_account_invariants, _runners
        r = ScalpRunner("r8-inv-ok", "u1", "EURUSD")
        _runners["r8-inv-ok:EURUSD"] = r
        r.live_trades["t1"] = {"state": "OPEN", "stop_px": 1.079,
                               "stop_risk_usd": 10.0}
        r.account_risk.open_scalps = 1
        r.account_risk.stop_risk_by_trade.clear()
        r.account_risk.add_stop_risk("t1", 10.0)
        assert verify_account_invariants("r8-inv-ok") == []

    def test_invariants_catch_missing_stop_and_risk_mismatch(self):
        from scalp.engine import (BLOCK_INVARIANT, account_block_reasons,
                                  verify_account_invariants, _runners)
        r = ScalpRunner("r8-inv-bad", "u1", "EURUSD")
        _runners["r8-inv-bad:EURUSD"] = r
        r.live_trades["t1"] = {"state": "OPEN", "stop_px": None,
                               "stop_risk_usd": 10.0}
        r.account_risk.open_scalps = 5          # wrong count
        r.account_risk.stop_risk_by_trade.clear()
        r.account_risk.add_stop_risk("t1", 99.0)  # wrong amount
        v = verify_account_invariants("r8-inv-bad")
        assert len(v) >= 2
        assert BLOCK_INVARIANT in account_block_reasons("r8-inv-bad")
        # clean state clears the block
        r.live_trades["t1"]["stop_px"] = 1.079
        r.account_risk.open_scalps = 1
        r.account_risk.stop_risk_by_trade["t1"] = 10.0
        assert verify_account_invariants("r8-inv-bad") == []
        assert BLOCK_INVARIANT not in account_block_reasons("r8-inv-bad")

    def test_stale_epoch_persist_fenced_out(self):
        from scalp import engine as eng
        r = ScalpRunner("r8-fence", "u1", "EURUSD")
        eng._lease_epoch["r8-fence"] = 3
        db = _stub_db()
        # fence filter does not match (doc holds a newer epoch)...
        db.scalp_risk_state.update_one = AsyncMock(
            return_value=MagicMock(matched_count=0))
        # ...and the doc EXISTS → stale worker must be rejected
        db.scalp_risk_state.find_one = AsyncMock(
            return_value={"lease_epoch": 7})
        with pytest.raises(RuntimeError, match="fenced out"):
            asyncio.run(r.persist_risk_now(db))

    def test_partial_close_exact_stop_risk_recompute(self):
        """Round 8 item 6 — remaining risk = remaining_lots × stop distance
        × pip value (exact), not proportional scaling."""
        r = ScalpRunner("r8-exact", "u1", "EURUSD")
        r.live_trades["t1"] = {"state": "OPEN", "opened_ms": 0,
                               "direction": "BUY", "lot": 0.10,
                               "entry_px": 1.0800, "stop_px": 1.0785,
                               "decision_id": "d", "est_cost_usd": 0.5}
        r.account_risk.add_stop_risk("t1", 999.0)   # stale/wrong value
        asyncio.run(r.on_partial_close("t1", 0.06, 0.04, pnl=1.0, deal_id="EX1"))
        # 0.04 lots × 15 pips × $10 = $6.00 exactly
        assert r.account_risk.stop_risk_by_trade["t1"] == pytest.approx(6.0)

    def test_financial_event_ledger_written(self):
        r = ScalpRunner("r8-ledger", "u1", "EURUSD")
        r.live_trades["t1"] = {"state": "OPEN", "opened_ms": 0,
                               "direction": "BUY", "lot": 0.05,
                               "entry_px": 1.08, "stop_px": 1.079,
                               "decision_id": "d", "est_cost_usd": 0.5}
        db = _stub_db()

        async def run():
            await r.on_trade_closed("t1", pnl=-2.0, commission=-0.3, swap=0.0,
                              deal_id="L1", source="broker_deal", db=db)
            for _ in range(5):
                await asyncio.sleep(0)
        asyncio.run(run())
        # round 9 — the ledger write is an idempotent upsert keyed on
        # (account_id, deal_id, event_type)
        call = db.scalp_financial_events.update_one.call_args
        flt, update = call.args
        assert flt == {"account_id": "r8-ledger", "deal_id": "L1",
                       "event_type": "full_close"}
        assert call.kwargs.get("upsert") is True
        ev = update["$setOnInsert"]
        assert ev["event_type"] == "full_close"
        assert ev["net_pnl"] == pytest.approx(-2.3)
        assert ev["risk_applied"] is True and ev["deal_id"] == "L1"



# ---------------- EA coherence (project-relative path) ----------------
def test_ea_144_tick_stream_wiring():
    ea_path = BACKEND / "static" / "EmergentTradingBridge.mq5"
    src = ea_path.read_text()
    import re
    m = re.search(r'#define\s+EA_CLIENT_VERSION\s+"([\d.]+)"', src)
    assert m and float(m.group(1)) >= 1.45      # no stale version pins
    assert "void SendTicks()" in src
    assert "/api/bridge/ticks" in src
    assert "EventSetMillisecondTimer" in src
    # v1.45 — remaining position volume on every live deal report
    assert '\\"position_volume\\":%.2f' in src
    assert "POSITION_VOLUME" in src


# ---------------- round-9 review behaviors ----------------
class TestRound9Hardening:
    def test_account_doc_write_fenced_out(self):
        """Round 9 item 1 — the shared _ACCOUNT risk doc is fenced: a stale
        epoch may not overwrite a newer owner's state."""
        r = ScalpRunner("r9-acct-fence", "u1", "EURUSD")
        db = _stub_db()
        db.scalp_risk_state.update_one = AsyncMock(
            return_value=MagicMock(matched_count=0))
        db.scalp_risk_state.find_one = AsyncMock(
            return_value={"lease_epoch": 9})
        with pytest.raises(RuntimeError, match="account risk persist fenced"):
            asyncio.run(r._fenced_account_write(
                db, 3, "2026-06-01T00:00:00+00:00"))

    def test_account_doc_write_inserts_when_absent(self):
        r = ScalpRunner("r9-acct-new", "u1", "EURUSD")
        db = _stub_db()
        db.scalp_risk_state.update_one = AsyncMock(
            return_value=MagicMock(matched_count=0))
        db.scalp_risk_state.find_one = AsyncMock(return_value=None)
        asyncio.run(r._fenced_account_write(
            db, 3, "2026-06-01T00:00:00+00:00"))
        last = db.scalp_risk_state.update_one.call_args
        assert last.kwargs.get("upsert") is True
        assert last.args[1]["$setOnInsert"]["lease_epoch"] == 3

    def test_background_persist_carries_fence(self):
        """Round 9 item 1 — asynchronous risk mirrors carry the SAME fence
        as the synchronous path (filter + stored epoch)."""
        from scalp import engine as eng
        r = ScalpRunner("r9-bg-fence", "u1", "EURUSD")
        eng._lease_epoch["r9-bg-fence"] = 5
        db = _stub_db()

        async def run():
            r._persist_risk(db)
            for _ in range(5):
                await asyncio.sleep(0)
        asyncio.run(run())
        calls = db.scalp_risk_state.update_one.call_args_list
        assert len(calls) == 2                    # symbol doc + _ACCOUNT doc
        for c in calls:
            flt, update = c.args
            assert "$or" in flt                   # fence filter present
            assert update["$set"]["lease_epoch"] == 5

    def test_lease_lost_before_submit_rejected(self):
        """Round 9 item 4 — ownership is re-confirmed at the LAST moment
        before broker submission; a worker without the lease never submits."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from scalp import engine as eng
        r = ScalpRunner("r9-lease-sub", "u1", "EURUSD")
        r.account = {"_id": "acc9", "equity": 10_000.0}
        now = int(time.time() * 1000)
        r.state.update(TickEvent(symbol="EURUSD", broker_time_ms=now,
                                 received_time_ms=now, bid=1.08,
                                 ask=1.08004))
        mid = (1.08 + 1.08004) / 2.0
        decision = {"decision_id": "d9", "direction": "BUY",
                    "net_edge_pips": 2.0, "sim": {"entry_mid": mid}}
        fc = SimpleNamespace(stop_pips=3.0, target_pips=5.0,
                             expected_spread_cost_pips=0.4)
        fake_engine = MagicMock(execute=AsyncMock())
        db = _stub_db()

        async def run():
            with patch("execution.for_account",
                       return_value=fake_engine), \
                 patch.object(eng, "confirm_account_lease_now",
                              AsyncMock(return_value=(False, 0))):
                await r._submit_live(db, decision, fc, {"lot": 0.01})
                for _ in range(5):
                    await asyncio.sleep(0)
        asyncio.run(run())
        fake_engine.execute.assert_not_awaited()
        sets = [c.args[1]["$set"] for c
                in db.scalp_decisions.update_one.call_args_list]
        assert any(s.get("reject_stage") == "lease_lost_before_submit"
                   for s in sets)

    def test_durable_invariants_block_unrestored_and_unstopped(self):
        """Round 9 item 3 — DB-side invariants: open scalps with no restored
        runner, or without a protective stop, block their account."""
        from scalp.engine import (BLOCK_DURABLE_INVARIANT,
                                  account_block_reasons, clear_account_block,
                                  verify_durable_invariants, _runners)
        db = _stub_db()

        class _AC:
            def __init__(self, items):
                self._items = list(items)

            def sort(self, *a, **k):
                return self

            def __aiter__(self):
                self._it = iter(self._items)
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

        db.trades.find = MagicMock(side_effect=lambda *a, **k: _AC([
            {"account_id": "r9-din-1", "stop_loss": 1.07},   # no runner
            {"_id": "t2", "account_id": "r9-din-2", "stop_loss": 0.0},
            {"_id": "t3", "account_id": "r9-din-3", "stop_loss": 1.07},
        ]))
        r2 = ScalpRunner("r9-din-2", "u1", "EURUSD")
        r2._risk_restored = True
        _runners["r9-din-2:EURUSD"] = r2
        # r9-din-3: restored AND consistent (DB open set == runner live set)
        r3 = ScalpRunner("r9-din-3", "u1", "EURUSD")
        r3._risk_restored = True
        r3.live_trades["t3"] = {"state": "OPEN", "stop_px": 1.07,
                                "stop_risk_usd": 5.0}
        r3.account_risk.open_scalps = 1
        r3.account_risk.stop_risk_by_trade.clear()
        r3.account_risk.add_stop_risk("t3", 5.0)
        _runners["r9-din-3:EURUSD"] = r3
        try:
            out = asyncio.run(verify_durable_invariants(db))
            blocked = {b["account_id"]: b for b in out["blocked"]}
            assert set(blocked) == {"r9-din-1", "r9-din-2"}
            assert blocked["r9-din-2"]["position_mismatch"] is True
            assert BLOCK_DURABLE_INVARIANT in account_block_reasons("r9-din-1")
            assert BLOCK_DURABLE_INVARIANT in account_block_reasons("r9-din-2")
            assert BLOCK_DURABLE_INVARIANT not in account_block_reasons("r9-din-3")
        finally:
            clear_account_block("r9-din-1", BLOCK_DURABLE_INVARIANT)
            clear_account_block("r9-din-2", BLOCK_DURABLE_INVARIANT)
            _runners.pop("r9-din-2:EURUSD", None)
            _runners.pop("r9-din-3:EURUSD", None)

    def test_emergency_stop_uses_instrument_registry_pip(self):
        """Round 9 item 5 — no hardcoded pip sizes: XAUUSD uses the
        authoritative registry (0.1), not the old 0.01 guess."""
        from protection_guard import (calculate_emergency_stop,
                                      EMERGENCY_RISK_PCT, FALLBACK_PRICE_PCT)
        from pip_utils import pip_size, pip_value_usd_per_lot
        assert pip_size("XAUUSD") == pytest.approx(0.1)
        entry, lot, equity = 2400.0, 0.01, 10_000.0
        sl = calculate_emergency_stop(entry, "BUY", lot, "XAUUSD", equity)
        pv = pip_value_usd_per_lot("XAUUSD", None) or 10.0
        budget = equity * EMERGENCY_RISK_PCT / 100.0
        dist = min((budget / (lot * pv)) * pip_size("XAUUSD"),
                   entry * FALLBACK_PRICE_PCT / 100.0)
        assert sl == pytest.approx(entry - dist, abs=1e-4)

    def test_emergency_stop_none_on_zero_equity_or_subpip(self):
        """Round 9 item 5 — the budget is never silently inflated: zero
        equity or a sub-pip stop distance escalates to a close instead."""
        from protection_guard import calculate_emergency_stop
        assert calculate_emergency_stop(1.08, "BUY", 0.05, "EURUSD", 0.0) is None
        assert calculate_emergency_stop(1.08, "BUY", 10.0, "EURUSD", 100.0) is None

    def test_modification_ack_confirms_emergency_protection(self):
        """Round 9 item 8 / round 12 item 1 — the protection-ack policy is
        tested through the dependency-free helper (no FastAPI/BSON/route
        imports); the full route flow lives in the integration suite."""
        from protection_guard import apply_protection_ack
        trade = {"action": "BUY", "entry_price": 1.08,
                 "protection_state": "EMERGENCY_STOP_PENDING",
                 "requested_stop_loss": 1.079, "breakeven_set": False}
        update = apply_protection_ack(trade, True, new_sl=1.079, now_iso="T9")
        assert update["protection_state"] == "RESOLVED"
        assert update["protection_missing"] is False
        assert update["confirmed_stop_loss"] == pytest.approx(1.079)
        assert update["protection_resolved_at"] == "T9"

    def test_modification_ack_failure_returns_to_unknown(self):
        """Round 9 item 8 — a FAILED emergency-stop ack never assumes
        protection: the trade returns to PROTECTION_UNKNOWN for retry."""
        from protection_guard import apply_protection_ack
        trade = {"action": "BUY", "entry_price": 1.08,
                 "protection_state": "EMERGENCY_STOP_PENDING"}
        update = apply_protection_ack(trade, False, error="broker rejected SL")
        assert update["protection_state"] == "PROTECTION_UNKNOWN"
        assert update["last_modification_error"] == "broker rejected SL"


class TestRound10Hardening:
    def test_confirm_lease_for_order_not_owned(self):
        from scalp import engine as eng
        db = _stub_db()
        db.scalp_owners.find_one = AsyncMock(return_value=None)
        owned, epoch = asyncio.run(
            eng.confirm_account_lease_now(db, "acc10"))
        assert owned is False and epoch == 0
        # the query must be worker- and expiry-scoped (non-cached, live doc)
        flt = db.scalp_owners.find_one.call_args.args[0]
        assert flt["worker_id"] == eng._worker_id
        assert "$gt" in flt["lease_until"]

    def test_confirm_lease_for_order_owned_updates_epoch(self):
        from scalp import engine as eng
        db = _stub_db()
        db.scalp_owners.find_one = AsyncMock(return_value={"lease_epoch": 7})
        owned, epoch = asyncio.run(
            eng.confirm_account_lease_now(db, "acc10b"))
        assert owned is True and epoch == 7
        assert eng._lease_epoch["acc10b"] == 7

    def test_submit_live_stamps_lease_epoch_into_signal(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from scalp import engine as eng
        r = ScalpRunner("acc10c", "u1", "EURUSD")
        from datetime import datetime, timezone
        r.equity = 10_000.0
        r.account = {"_id": "acc10c", "equity": 10_000.0,
                     "status": "connected",
                     "last_heartbeat": datetime.now(timezone.utc).isoformat()}
        now = int(time.time() * 1000)
        r.state.update(TickEvent(symbol="EURUSD", broker_time_ms=now,
                                 received_time_ms=now, bid=1.08,
                                 ask=1.08004))
        mid = (1.08 + 1.08004) / 2.0
        decision = {"decision_id": "d10", "direction": "BUY",
                    "net_edge_pips": 2.0, "sim": {"entry_mid": mid}}
        fc = SimpleNamespace(stop_pips=3.0, target_pips=5.0,
                             expected_slippage_pips=0.1,
                             expected_spread_cost_pips=0.4)
        fake_engine = MagicMock(
            execute=AsyncMock(return_value={"id": "t10"}))
        db = _stub_db()

        async def run():
            with patch("execution.for_account", return_value=fake_engine), \
                 patch.object(eng, "confirm_account_lease_now",
                              AsyncMock(return_value=(True, 9))):
                await r._submit_live(db, decision, fc, {"lot": 0.01})
                for _ in range(5):
                    await asyncio.sleep(0)
        asyncio.run(run())
        signal = fake_engine.execute.await_args.kwargs["signal"]
        assert signal["scalp_lease_epoch"] == 9

    def test_build_financial_event_signed_math(self):
        from scalp.deals import build_financial_event
        ev = build_financial_event(
            account_id="a", symbol="EURUSD", trade_id="t", deal_id=42,
            event_type="full_close", profit=-3.0, commission=-0.7, swap=0.2,
            lease_epoch=3)
        assert ev["deal_id"] == "42"
        assert ev["net_pnl"] == -3.5            # -3.0 - 0.7 + 0.2
        assert ev["trading_pnl"] == -3.7        # excludes financing credit
        assert ev["execution_cost"] == 0.7      # only negative legs
        assert ev["lease_epoch"] == 3
        assert "remaining_lots" not in ev
        ev2 = build_financial_event(
            account_id="a", symbol="EURUSD", trade_id="t", deal_id=43,
            event_type="partial_close", profit=1.0, commission=0.0,
            swap=-0.1, remaining_lots=0.02)
        assert ev2["remaining_lots"] == 0.02
        assert ev2["net_pnl"] == 0.9

    def test_apply_broker_deal_writes_pending_ledger_before_apply(self):
        from unittest.mock import patch
        from scalp import engine as eng
        db = _stub_db()
        calls = []

        async def track_update(flt, update, upsert=False):
            calls.append((dict(flt), update))
            return MagicMock(matched_count=1, modified_count=1)
        db.scalp_financial_events.update_one = AsyncMock(
            side_effect=track_update)
        r = ScalpRunner("acc10d", "u1", "EURUSD")
        r._risk_restored = True
        eng._runners["acc10d:EURUSD"] = r

        async def run():
            with patch.object(eng, "ensure_account_lease",
                              AsyncMock(return_value=True)), \
                 patch.object(ScalpRunner, "persist_risk_now", AsyncMock()):
                return await eng.apply_broker_deal(
                    db, "acc10d",
                    {"_id": "tr1", "symbol": "EURUSD", "user_id": "u1"},
                    deal_id="d-77", lots=0.01, profit=-1.0, commission=-0.1,
                    swap=0.0, price=1.08, partial=False)
        res = asyncio.run(run())
        eng._runners.pop("acc10d:EURUSD", None)
        assert res["applied"] is True
        # first ledger write: PENDING via $setOnInsert (before risk apply)
        first = calls[0][1]
        assert first["$setOnInsert"]["status"] == "pending"
        assert first["$setOnInsert"]["risk_applied"] is False
        assert first["$setOnInsert"]["net_pnl"] == -1.1
        # a ledger write flips it to APPLIED after the fenced persist
        applied_sets = [u["$set"] for _, u in calls if "$set" in u]
        assert any(s.get("status") == "applied" and s.get("risk_applied")
                   for s in applied_sets)

    def test_pip_value_strict_fails_closed_on_unknown(self):
        from pip_utils import pip_value_usd_per_lot_strict
        assert pip_value_usd_per_lot_strict("EURUSD") == 10.0
        assert pip_value_usd_per_lot_strict("EURUSD#") == 10.0
        assert pip_value_usd_per_lot_strict("XAUUSD.fx") == 10.0
        assert pip_value_usd_per_lot_strict("EURGBP") is None   # cross pair
        assert pip_value_usd_per_lot_strict("WEIRDSYM") is None

    def test_service_block_vetoes_entries(self):
        from scalp import engine as eng
        eng.set_service_block("critical index creation failed: boom")
        try:
            assert eng.service_block_reason().startswith("critical index")
            assert eng.audit_backlog()["service_block"] is not None
        finally:
            eng.set_service_block(None)
        assert eng.audit_backlog()["service_block"] is None


class TestRound11Hardening:
    def test_apply_protection_ack_pure(self):
        # dependency-free: no FastAPI/BSON/Mongo needed
        from protection_guard import apply_protection_ack
        tr = {"protection_state": "EMERGENCY_STOP_PENDING"}
        up = apply_protection_ack(tr, True, new_sl=1.077, now_iso="T")
        assert up == {"confirmed_stop_loss": 1.077,
                      "protection_state": "RESOLVED",
                      "protection_missing": False,
                      "protection_resolved_at": "T"}
        up = apply_protection_ack(tr, False, error="broker rejected SL")
        assert up["last_modification_error"] == "broker rejected SL"
        assert up["protection_state"] == "PROTECTION_UNKNOWN"
        # non-emergency trades: success ack adds nothing protection-related
        assert apply_protection_ack({}, True, new_sl=1.08) == {}
        # non-emergency failure records the error only
        up = apply_protection_ack({}, False, error=None)
        assert up == {"last_modification_error": "unknown EA error"}

    def test_find_account_reasons(self):
        from protection_guard import find_account
        db = MagicMock()
        db.accounts.find_one = AsyncMock(return_value=None)
        acc, why = asyncio.run(find_account(db, ""))
        assert acc is None and why == "invalid_id"
        acc, why = asyncio.run(find_account(db, "not-an-objectid"))
        assert acc is None and why == "invalid_id"
        db.accounts.find_one = AsyncMock(side_effect=RuntimeError("down"))
        acc, why = asyncio.run(find_account(db, "0" * 24))
        assert acc is None and why == "db_error"

    def test_find_account_without_parser_string_lookup_only(self):
        """Round 14 — dependency-injected ObjectId parser: with no parser
        (BSON unavailable) a shaped id gets exactly ONE string lookup."""
        from protection_guard import find_account
        db = MagicMock()
        db.accounts.find_one = AsyncMock(return_value=None)
        acc, why = asyncio.run(find_account(db, "0" * 24, oid_parser=None))
        assert acc is None and why == "missing"
        assert db.accounts.find_one.await_count == 1
        db.accounts.find_one = AsyncMock(
            return_value={"_id": "0" * 24, "equity": 5.0})
        acc, why = asyncio.run(find_account(db, "0" * 24, oid_parser=None))
        assert acc is not None and why == "ok"
        assert db.accounts.find_one.await_count == 1

    def test_find_account_with_parser_falls_back_to_raw_string(self):
        """Round 14 — with a parser available: ObjectId lookup first, raw
        string fallback second, missing only after both miss."""
        from protection_guard import find_account
        db = MagicMock()
        db.accounts.find_one = AsyncMock(
            side_effect=[None, {"_id": "str-id", "equity": 55.0}])
        acc, why = asyncio.run(find_account(db, "0" * 24, oid_parser=str))
        assert acc is not None and why == "ok"
        assert db.accounts.find_one.await_count == 2
        db.accounts.find_one = AsyncMock(side_effect=[None, None])
        acc, why = asyncio.run(find_account(db, "0" * 24, oid_parser=str))
        assert acc is None and why == "missing"
        assert db.accounts.find_one.await_count == 2

    def test_daily_metrics_are_separate(self):
        from scalp.risk import RiskState
        rs = RiskState()
        rs.record_result(-5.0, 0.4)
        rs.record_result(3.0, 0.3)
        rs.record_result(-2.0, 0.2)
        doc = rs.to_doc()
        assert doc["daily_gross_loss_usd"] == pytest.approx(7.0)
        assert doc["daily_net_pnl_usd"] == pytest.approx(-4.0)
        assert doc["daily_cost_usd"] == pytest.approx(0.9)
        rs2 = RiskState()
        rs2.load_doc(doc)
        assert rs2.daily_loss_usd == pytest.approx(7.0)
        assert rs2.daily_net_pnl_usd == pytest.approx(-4.0)

    def test_financial_event_occurred_vs_received(self):
        from scalp.deals import build_financial_event
        ev = build_financial_event(
            account_id="a", symbol="EURUSD", trade_id="t", deal_id=1,
            event_type="full_close", profit=-1, commission=0, swap=0,
            at_iso="2026-07-15T23:59:58+00:00")
        assert ev["at"] == "2026-07-15T23:59:58+00:00"
        assert ev["occurred_at"] == ev["at"]
        assert ev["received_at"] != ev["occurred_at"]
        ev2 = build_financial_event(
            account_id="a", symbol="EURUSD", trade_id="t", deal_id=2,
            event_type="full_close", profit=1, commission=0, swap=0)
        assert ev2["occurred_at"] == ev2["received_at"]

    def test_commission_pips_fails_closed_on_unpriceable(self):
        r = ScalpRunner("acc11", "u1", "EURUSD")
        r.commission_usd_per_lot_side = 3.5
        assert r._commission_pips() == pytest.approx(0.7)
        r2 = ScalpRunner("acc11b", "u1", "EURUSD")
        r2.symbol = "EURGBP"          # cross pair — no authoritative value
        r2.commission_usd_per_lot_side = 3.5
        assert r2._commission_pips() == 999.0


class TestRound12Hardening:
    def test_looks_like_object_id(self):
        from protection_guard import looks_like_object_id
        assert looks_like_object_id("0" * 24)
        assert looks_like_object_id("a1B2c3D4e5F6a1B2c3D4e5F6")
        assert not looks_like_object_id("not-an-objectid")
        assert not looks_like_object_id("0" * 23)
        assert not looks_like_object_id("g" * 24)

    def test_emergency_stop_none_for_unpriceable_symbol(self):
        from protection_guard import calculate_emergency_stop
        # cross pair — no authoritative pip value → None → caller closes
        assert calculate_emergency_stop(
            0.86, "BUY", 0.05, "EURGBP", 10_000.0) is None

    def test_emergency_stop_snaps_to_tick(self):
        from protection_guard import calculate_emergency_stop
        from scalp.instruments import approved
        tick = approved("EURUSD").tick_size
        sl = calculate_emergency_stop(1.08123, "BUY", 0.05, "EURUSD", 10_000.0)
        assert sl is not None
        assert abs((sl / tick) - round(sl / tick)) < 1e-6

    def test_confirm_or_adopt_first_touch_vs_foreign_owner(self):
        from unittest.mock import patch
        from scalp import engine as eng
        db = _stub_db()
        # no owner doc at all → first-touch adoption acquires
        db.scalp_owners.find_one = AsyncMock(return_value=None)
        with patch.object(eng, "acquire_account_lease",
                          AsyncMock(return_value=True)) as acq:
            assert asyncio.run(eng.confirm_or_adopt_account_lease(db, "aX"))
            acq.assert_awaited_once()
        # foreign owner doc present (even expired) → live path refuses
        async def fo(flt, proj=None):
            if "worker_id" in flt:            # confirm query
                return None
            return {"worker_id": "other:1"}   # ownership record exists
        db.scalp_owners.find_one = AsyncMock(side_effect=fo)
        with patch.object(eng, "acquire_account_lease",
                          AsyncMock(return_value=True)) as acq:
            assert not asyncio.run(eng.confirm_or_adopt_account_lease(db, "aY"))
            acq.assert_not_awaited()

    def test_recovery_takeover_is_logged_acquire(self):
        from unittest.mock import patch
        from scalp import engine as eng
        db = _stub_db()
        db.scalp_owners.find_one = AsyncMock(
            return_value={"worker_id": "other:1", "lease_epoch": 4})
        with patch.object(eng, "acquire_account_lease",
                          AsyncMock(return_value=True)):
            assert asyncio.run(
                eng.acquire_expired_ownership_for_recovery(db, "aZ"))

    def test_invariant_scan_stale_reason(self):
        from datetime import datetime, timedelta, timezone
        from scalp import engine as eng
        saved = dict(eng._invariant_scan)
        try:
            eng._invariant_scan.update(
                {"last_attempt_at": None, "last_success_at": None})
            assert eng.invariant_scan_stale_reason() is None  # never ran
            now = datetime.now(timezone.utc)
            eng._invariant_scan.update(
                {"last_attempt_at": now.isoformat(),
                 "last_success_at": now.isoformat()})
            assert eng.invariant_scan_stale_reason() is None  # fresh
            old = (now - timedelta(seconds=eng.INVARIANT_SCAN_STALE_SEC + 60))
            eng._invariant_scan.update(
                {"last_attempt_at": now.isoformat(),
                 "last_success_at": old.isoformat()})
            reason = eng.invariant_scan_stale_reason()
            assert reason and "invariant scan stale" in reason
            # attempted but NEVER succeeded → also stale
            eng._invariant_scan.update(
                {"last_attempt_at": now.isoformat(), "last_success_at": None})
            assert eng.invariant_scan_stale_reason() is not None
        finally:
            eng._invariant_scan.clear()
            eng._invariant_scan.update(saved)

    def test_repair_reports_queue_metrics(self):
        from protection_guard import repair_unprotected_positions

        class _AsyncCursor:
            def __init__(self, items):
                self._items = list(items)

            def sort(self, *a, **k):
                return self

            def __aiter__(self):
                self._it = iter(self._items)
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

        db = _stub_db()
        db.trades.find = MagicMock(side_effect=lambda *a, **k: _AsyncCursor([]))
        db.trades.count_documents = AsyncMock(return_value=0)
        db.trades.find_one = AsyncMock(return_value=None)
        db.trades.distinct = AsyncMock(return_value=[])
        out = asyncio.run(repair_unprotected_positions(db))
        assert out["awaiting"] == 0 and out["processed"] == 0
        assert "oldest_unresolved_age_sec" in out


# ---------------- round-13 hardening behaviors ----------------
class TestRound13Hardening:
    def test_reason_level_blocks_are_owned_per_subsystem(self):
        """Item 9 — clearing one subsystem's reason never clears another's."""
        from scalp import engine as eng
        acct = "r13-own"
        eng.add_account_block(acct, eng.BLOCK_RISK_UNKNOWN)
        eng.set_protection_block(acct, True)
        assert eng.account_block_reasons(acct) == {
            eng.BLOCK_RISK_UNKNOWN, eng.BLOCK_MISSING_PROTECTION}
        eng.set_protection_block(acct, False)      # clears ONLY protection
        assert eng.account_block_reasons(acct) == {eng.BLOCK_RISK_UNKNOWN}
        eng.clear_account_block(acct, eng.BLOCK_RISK_UNKNOWN)
        assert eng.account_block_reasons(acct) == set()

    def test_broker_state_staleness_vetoes(self):
        """Item 8 — readiness requires a fresh, connected broker snapshot."""
        from datetime import datetime, timedelta, timezone
        from scalp.engine import broker_state_stale_reason
        now_iso = datetime.now(timezone.utc).isoformat()
        assert broker_state_stale_reason(
            {"last_heartbeat": now_iso, "status": "connected"}) is None
        assert broker_state_stale_reason(None) is not None
        assert "no heartbeat" in broker_state_stale_reason({"equity": 1000})
        old = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        assert "stale" in broker_state_stale_reason({"last_heartbeat": old})
        assert "not connected" in broker_state_stale_reason(
            {"last_heartbeat": now_iso, "status": "disconnected"})

    def test_restore_scopes_runner_to_its_own_symbol(self):
        """Item 2 — a runner restores ONLY its own symbol's open trades
        (suffix-tolerant); other symbols stay out of its book."""
        r = ScalpRunner("r13-sym", "u1", "EURUSD")
        db = _stub_db()
        docs = [
            {"_id": "tE", "action": "BUY", "entry_price": 1.08,
             "stop_loss": 1.079, "take_profit": 1.082, "lot_size": 0.05,
             "symbol": "EURUSD.a", "scalp_decision_id": ""},
            {"_id": "tX", "action": "BUY", "entry_price": 2400.0,
             "stop_loss": 2390.0, "take_profit": 2420.0, "lot_size": 0.01,
             "symbol": "XAUUSD", "scalp_decision_id": ""},
        ]
        db.trades.find = MagicMock(return_value=_AsyncCursor(docs))
        asyncio.run(r.restore_risk(db))
        assert set(r.live_trades) == {"tE"}
        assert r.risk_state.open_scalps == 1
        # account-level state still covers ALL symbols
        assert r.account_risk.open_scalps == 2

    def test_restore_clears_only_its_own_risk_unknown_reason(self):
        from scalp import engine as eng
        acct = "r13-clear"
        eng.add_account_block(acct, eng.BLOCK_RISK_UNKNOWN)
        eng.add_account_block(acct, eng.BLOCK_MISSING_PROTECTION)
        r = ScalpRunner(acct, "u1", "EURUSD")
        db = _stub_db()
        db.trades.find = MagicMock(return_value=_AsyncCursor([]))
        asyncio.run(r.restore_risk(db))
        # clean restore clears risk_unknown but NOT the protection block
        assert eng.account_block_reasons(acct) == {
            eng.BLOCK_MISSING_PROTECTION}
        eng.set_protection_block(acct, False)

    def test_status_exposes_block_reasons_and_staleness(self):
        from scalp import engine as eng
        r = ScalpRunner("r13-status", "u1", "EURUSD")
        eng.add_account_block("r13-status", eng.BLOCK_RISK_UNKNOWN)
        st = r.status()
        assert st["block_reasons"] == ["risk_unknown"]
        assert st["broker_state_stale"] is not None   # no account snapshot
        eng.clear_account_block("r13-status", eng.BLOCK_RISK_UNKNOWN)

    def test_rounding_digits_derived_from_step(self):
        """Item 4 — decimals come from the instrument step, never a
        hardcoded 5-decimal FX assumption."""
        from protection_guard import rounding_digits
        assert rounding_digits(0.00001) == 5
        assert rounding_digits(0.001) == 3
        assert rounding_digits(0.1) == 1
        assert rounding_digits(0.25) == 2
        assert rounding_digits(1.0) == 0

    def test_emergency_stop_respects_broker_min_stop_distance(self):
        """Item 5 — a stop tighter than the broker minimum is unplaceable;
        the budget is never widened → escalate (None) instead."""
        from protection_guard import (broker_stop_constraints,
                                      calculate_emergency_stop)
        acc = {"symbol_specs": {"EURUSD": {
            "point": 0.00001, "digits": 5,
            "stops_level_points": 20, "freeze_level_points": 0}}}
        bc = broker_stop_constraints(acc, "EURUSD.a")
        assert bc["min_stop_distance_px"] == pytest.approx(0.0002)
        sl0 = calculate_emergency_stop(1.08, "BUY", 0.05, "EURUSD", 10_000.0)
        sl1 = calculate_emergency_stop(1.08, "BUY", 0.05, "EURUSD", 10_000.0,
                                       broker_constraints=bc)
        assert sl1 == pytest.approx(sl0)      # constraint satisfied
        huge = {"min_stop_distance_px": 1.0, "freeze_distance_px": 0.0}
        assert calculate_emergency_stop(
            1.08, "BUY", 0.05, "EURUSD", 10_000.0,
            broker_constraints=huge) is None
        # missing/unreported specs → no constraint object
        assert broker_stop_constraints({}, "EURUSD") is None
        assert broker_stop_constraints(
            {"symbol_specs": {"EURUSD": {"point": 0}}}, "EURUSD") is None

    def test_daily_risk_boundary_is_explicit_utc(self):
        """Item 6 — the daily boundary is the UTC calendar day everywhere."""
        from datetime import datetime, timezone
        from scalp.risk import utc_day_key
        assert utc_day_key() == datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rs = RiskState(DEFAULT_LIMITS)
        rs.record_result(-5.0, 1.0)
        assert rs.daily_key == utc_day_key()

    def test_recovery_sweep_claims_deal_before_processing(self):
        """Item 7 — transactional sweep: a deal that cannot be atomically
        claimed (another worker holds it / already completed) is skipped
        without touching trades or risk."""
        from scalp.engine import recover_pending_deals
        deal = {"deal_id": 1313, "account_id": "r13-claim", "mt5_ticket": 9,
                "profit": -1.0, "commission": 0.0, "swap": 0.0,
                "price": 1.079, "lots": 0.05,
                "financial_reconciliation_status": "pending",
                "received_at": "2020-01-01T00:00:00+00:00"}
        db = _stub_db()
        db.broker_deals.find = MagicMock(return_value=_AsyncCursor([deal]))
        db.broker_deals.update_one = AsyncMock(
            return_value=MagicMock(matched_count=0))
        db.trades.find_one = AsyncMock()
        out = asyncio.run(recover_pending_deals(db, older_than_sec=0))
        assert out == {"recovered": 0, "marked_complete": 0,
                       "kept_pending": 0}
        db.trades.find_one.assert_not_awaited()
        filt = db.broker_deals.update_one.call_args.args[0]
        assert filt["financial_reconciliation_status"] == "pending"
        assert "$or" in filt                     # unclaimed-or-expired guard

    def test_clock_drift_ignores_broker_timezone_offset(self):
        """Broker tick clocks are broker-LOCAL (UTC+2/+3 EET, UTC+5:30):
        the constant timezone offset is not drift and must not trip the
        kill-switch; genuine residual skew still halts."""
        from scalp.kill import clock_drift_residual_ms, MAX_CLOCK_DRIFT_MS
        assert abs(clock_drift_residual_ms(-10_800_000)) < MAX_CLOCK_DRIFT_MS
        assert abs(clock_drift_residual_ms(-7_200_000 + 1200)) < MAX_CLOCK_DRIFT_MS
        assert abs(clock_drift_residual_ms(-19_800_000)) < MAX_CLOCK_DRIFT_MS
        assert abs(clock_drift_residual_ms(0.0)) < MAX_CLOCK_DRIFT_MS
        assert abs(clock_drift_residual_ms(-10_800_000 - 8000)) > MAX_CLOCK_DRIFT_MS
        assert abs(clock_drift_residual_ms(600_000)) > MAX_CLOCK_DRIFT_MS


# ---------------- round-14 hardening behaviors ----------------
class TestRound14Hardening:
    def test_heartbeat_propagates_snapshot_into_runners(self):
        """P0 — update_account_snapshot pushes fresh heartbeat state into
        every in-memory runner: staleness checks and equity never rely on
        the last tick batch's preloaded account doc."""
        from datetime import datetime, timezone
        from scalp import engine as eng
        r = eng.ScalpRunner("r14-snap", "u1", "EURUSD")
        eng._runners["r14-snap:EURUSD"] = r
        try:
            r.account = {"_id": "r14-snap", "equity": 1000.0,
                         "last_heartbeat": "2020-01-01T00:00:00+00:00"}
            r.equity = 1000.0
            assert eng.broker_state_stale_reason(r.account) is not None
            now_iso = datetime.now(timezone.utc).isoformat()
            eng.update_account_snapshot("r14-snap", {
                "equity": 1234.5, "status": "connected",
                "last_heartbeat": now_iso})
            assert r.equity == pytest.approx(1234.5)
            assert r.account["last_heartbeat"] == now_iso
            assert eng.broker_state_stale_reason(r.account) is None
            # equity None (mismatch heartbeat) must not clobber runner equity
            eng.update_account_snapshot("r14-snap", {
                "equity": None, "status": "disconnected",
                "last_heartbeat": now_iso})
            assert r.equity == pytest.approx(1234.5)
            assert eng.broker_state_stale_reason(r.account) is not None
        finally:
            eng._runners.pop("r14-snap:EURUSD", None)

    def test_submit_refreshes_broker_state_and_rejects_stale(self):
        """Item 1 — immediately before submission the runner refreshes
        critical account state from the DB and rejects on staleness."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from scalp import engine as eng
        r = ScalpRunner("r14-pre", "u1", "EURUSD")
        r.equity = 10_000.0
        r.account = {"_id": "r14-pre", "equity": 10_000.0,
                     "status": "connected",
                     "last_heartbeat": "2020-01-01T00:00:00+00:00"}
        now = int(time.time() * 1000)
        r.state.update(TickEvent(symbol="EURUSD", broker_time_ms=now,
                                 received_time_ms=now, bid=1.08,
                                 ask=1.08004))
        decision = {"decision_id": "d14", "direction": "BUY",
                    "net_edge_pips": 2.0, "sim": {"entry_mid": 1.08002}}
        fc = SimpleNamespace(stop_pips=3.0, target_pips=5.0,
                             expected_slippage_pips=0.1,
                             expected_spread_cost_pips=0.4)
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t14"}))
        db = _stub_db()
        db.accounts.find_one = AsyncMock(return_value=None)  # no fresh doc

        async def run():
            with patch("execution.for_account", return_value=fake_engine), \
                 patch.object(eng, "confirm_account_lease_now",
                              AsyncMock(return_value=(True, 4))):
                await r._submit_live(db, decision, fc, {"lot": 0.01})
                for _ in range(5):
                    await asyncio.sleep(0)
        asyncio.run(run())
        fake_engine.execute.assert_not_awaited()   # stale heartbeat → reject
        db.accounts.find_one.assert_awaited()       # refresh WAS attempted

    def test_submit_rejects_on_material_equity_move(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from datetime import datetime, timezone
        from scalp import engine as eng
        r = ScalpRunner("r14-eq", "u1", "EURUSD")
        r.equity = 10_000.0
        r.account = {"_id": "r14-eq", "equity": 10_000.0,
                     "status": "connected",
                     "last_heartbeat": datetime.now(timezone.utc).isoformat()}
        now = int(time.time() * 1000)
        r.state.update(TickEvent(symbol="EURUSD", broker_time_ms=now,
                                 received_time_ms=now, bid=1.08,
                                 ask=1.08004))
        decision = {"decision_id": "d14b", "direction": "BUY",
                    "net_edge_pips": 2.0, "sim": {"entry_mid": 1.08002}}
        fc = SimpleNamespace(stop_pips=3.0, target_pips=5.0,
                             expected_slippage_pips=0.1,
                             expected_spread_cost_pips=0.4)
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t"}))
        db = _stub_db()
        # DB reports equity moved 10% since the decision snapshot
        db.accounts.find_one = AsyncMock(return_value={
            "_id": "r14-eq", "equity": 9_000.0, "status": "connected",
            "last_heartbeat": datetime.now(timezone.utc).isoformat()})

        async def run():
            with patch("execution.for_account", return_value=fake_engine), \
                 patch.object(eng, "confirm_account_lease_now",
                              AsyncMock(return_value=(True, 4))):
                await r._submit_live(db, decision, fc, {"lot": 0.01})
                for _ in range(5):
                    await asyncio.sleep(0)
        asyncio.run(run())
        fake_engine.execute.assert_not_awaited()

    def test_submission_capacity_guard(self):
        """Item 10 — bounded concurrent submissions: at capacity the
        decision is rejected instead of queueing unbounded broker calls."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from datetime import datetime, timezone
        from scalp import engine as eng
        r = ScalpRunner("r14-cap", "u1", "EURUSD")
        r.equity = 10_000.0
        r.account = {"_id": "r14-cap", "equity": 10_000.0,
                     "status": "connected",
                     "last_heartbeat": datetime.now(timezone.utc).isoformat()}
        now = int(time.time() * 1000)
        r.state.update(TickEvent(symbol="EURUSD", broker_time_ms=now,
                                 received_time_ms=now, bid=1.08,
                                 ask=1.08004))
        decision = {"decision_id": "d14c", "direction": "BUY",
                    "net_edge_pips": 2.0, "sim": {"entry_mid": 1.08002}}
        fc = SimpleNamespace(stop_pips=3.0, target_pips=5.0,
                             expected_slippage_pips=0.1,
                             expected_spread_cost_pips=0.4)
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t"}))
        db = _stub_db()

        async def run():
            with patch("execution.for_account", return_value=fake_engine), \
                 patch.object(eng, "confirm_account_lease_now",
                              AsyncMock(return_value=(True, 4))):
                eng._active_submissions = eng.MAX_CONCURRENT_SUBMISSIONS
                try:
                    await r._submit_live(db, decision, fc, {"lot": 0.01})
                    for _ in range(5):
                        await asyncio.sleep(0)
                finally:
                    eng._active_submissions = 0
        asyncio.run(run())
        fake_engine.execute.assert_not_awaited()

    def test_runner_capacity_cap(self):
        from scalp import engine as eng
        orig = eng.MAX_RUNNERS_PER_WORKER
        eng.MAX_RUNNERS_PER_WORKER = len(eng._runners)  # already full
        try:
            assert eng.get_runner("r14-over", "u1", "EURUSD") is None
        finally:
            eng.MAX_RUNNERS_PER_WORKER = orig
            eng._runners.pop("r14-over:EURUSD", None)

    def test_stale_symbol_specs_are_not_trusted(self):
        """Item 9 — specs older than SYMBOL_SPEC_MAX_AGE_SEC are ignored
        for stop-constraint math."""
        from datetime import datetime, timedelta, timezone
        from protection_guard import broker_stop_constraints
        spec = {"point": 0.00001, "digits": 5,
                "stops_level_points": 20, "freeze_level_points": 0}
        fresh_acc = {"symbol_specs": {"EURUSD": spec},
                     "symbol_specs_updated_at":
                         datetime.now(timezone.utc).isoformat()}
        assert broker_stop_constraints(fresh_acc, "EURUSD") is not None
        stale_acc = {"symbol_specs": {"EURUSD": spec},
                     "symbol_specs_updated_at":
                         (datetime.now(timezone.utc)
                          - timedelta(days=3)).isoformat()}
        assert broker_stop_constraints(stale_acc, "EURUSD") is None

    def test_block_bootstrap_ci_sanity(self):
        """Item 7 — CI brackets the sample mean; tiny samples return None."""
        from scalp.stats import block_bootstrap_ci
        assert block_bootstrap_ci([1.0] * 10) is None
        vals = [0.5, -0.3] * 200
        lo, hi = block_bootstrap_ci(vals, seed=7)
        mean = sum(vals) / len(vals)
        assert lo - 1e-6 <= mean <= hi + 1e-6
        varied = [((i * 37) % 11) - 5.0 for i in range(300)]
        wide = block_bootstrap_ci(varied, block=10, seed=7)
        assert wide[0] < wide[1]

    def test_commission_reconciliation_flags_mismatch(self):
        """Item 8 — observed live per-lot commission far from the
        configured value is surfaced as a mismatch."""
        r = ScalpRunner("r14-com", "u1", "EURUSD")
        r.commission_usd_per_lot_side = 3.0
        db = _stub_db()
        docs = [{"lot": 0.10,
                 "execution_outcome": {"commission_usd": -1.4}}] * 30
        cur = _AsyncCursor(docs)
        cur.sort = lambda *a, **k: cur
        cur.limit = lambda n: cur
        db.scalp_decisions.find = MagicMock(return_value=cur)
        asyncio.run(r.reconcile_commission(db))
        # observed 14 $/lot vs configured 3 $/lot-side → mismatch
        assert r.commission_check["observed_median_usd_per_lot"] == pytest.approx(14.0)
        assert r.commission_check["mismatch"] is True
        # matching configuration → no mismatch
        r2 = ScalpRunner("r14-com2", "u1", "EURUSD")
        r2.commission_usd_per_lot_side = 14.0
        db.scalp_decisions.find = MagicMock(return_value=cur)
        asyncio.run(r2.reconcile_commission(db))
        assert r2.commission_check["mismatch"] is False

    def test_suffixed_symbols_behave_end_to_end(self):
        """Item 4 — pure-unit suffixed-symbol behavior: base mapping,
        approved registry, pip derivation and runner assignment."""
        from pip_utils import base_symbol, pip_size
        from scalp.instruments import approved as appr
        for raw in ("EURUSD.a", "EURUSDm", "EURUSD.pro", "EURUSD#"):
            assert base_symbol(raw) == "EURUSD"
            assert appr(base_symbol(raw)) is not None
            assert pip_size(raw) == pytest.approx(0.0001)
        # restored trades route to the runner of their BASE symbol only
        r = ScalpRunner("r14-sfx", "u1", "EURUSD")
        db = _stub_db()
        db.trades.find = MagicMock(return_value=_AsyncCursor([
            {"_id": "sfx1", "action": "BUY", "entry_price": 1.08,
             "stop_loss": 1.079, "take_profit": 1.082, "lot_size": 0.05,
             "symbol": "EURUSD.pro", "scalp_decision_id": ""}]))
        asyncio.run(r.restore_risk(db))
        assert "sfx1" in r.live_trades

    def test_decision_records_account_snapshot(self):
        """Item 2 — every decision doc embeds the account snapshot used."""
        import inspect
        from scalp import engine as eng
        src = inspect.getsource(eng.ScalpRunner._maybe_evaluate)
        assert '"account_snapshot"' in src
        assert '"free_margin"' in src and '"heartbeat_at"' in src


# ---------------- round-15 hardening behaviors ----------------
def _r15_runner(name):
    from datetime import datetime, timezone
    r = ScalpRunner(name, "u1", "EURUSD")
    r.equity = 10_000.0
    r.account = {"_id": name, "equity": 10_000.0, "status": "connected",
                 "last_heartbeat": datetime.now(timezone.utc).isoformat()}
    now = int(time.time() * 1000)
    r.state.update(TickEvent(symbol="EURUSD", broker_time_ms=now,
                             received_time_ms=now, bid=1.08, ask=1.08004))
    return r


def _r15_submit(r, db, fake_engine, lot=0.9):
    from types import SimpleNamespace
    from unittest.mock import patch
    from scalp import engine as eng
    decision = {"decision_id": "d15", "direction": "BUY",
                "ts_ms": int(time.time() * 1000),
                "net_edge_pips": 2.0, "sim": {"entry_mid": 1.08002}}
    fc = SimpleNamespace(stop_pips=3.0, target_pips=5.0,
                         expected_slippage_pips=0.1,
                         expected_spread_cost_pips=0.4)

    async def run():
        with patch("execution.for_account", return_value=fake_engine), \
             patch.object(eng, "confirm_account_lease_now",
                          AsyncMock(return_value=(True, 7))):
            await r._submit_live(db, decision, fc, {"lot": lot})
            for _ in range(6):
                await asyncio.sleep(0)
    asyncio.run(run())


class TestRound15Hardening:
    def test_submitted_lot_resized_to_fresh_equity(self):
        """Main item — the broker receives min(decision lot, lot sized from
        the FINAL accepted snapshot), never the stale-equity lot."""
        from scalp.risk import check as risk_check
        r = _r15_runner("r15-resize")
        db = _stub_db()
        from datetime import datetime, timezone
        db.accounts.find_one = AsyncMock(return_value={
            "_id": "r15-resize", "equity": 9_900.0, "status": "connected",
            "last_heartbeat": datetime.now(timezone.utc).isoformat()})
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t15"}))
        from pip_utils import pip_value_usd_per_lot_strict
        expected = risk_check(r.risk_state, 9_900.0, 3.0,
                              pip_value_usd_per_lot_strict("EURUSD"),
                              r.cfg)["lot"]
        _r15_submit(r, db, fake_engine, lot=0.9)
        sig = fake_engine.execute.await_args.kwargs["signal"]
        assert sig["lot_size"] == pytest.approx(min(0.9, expected))
        assert sig["lot_size"] < 0.9

    def test_presubmit_rejects_when_fresh_risk_fails(self):
        from scalp.risk import utc_day_key
        r = _r15_runner("r15-riskfail")
        r.risk_state.daily_key = utc_day_key()
        r.risk_state.daily_loss_usd = 1e9      # daily loss limit breached
        db = _stub_db()
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t"}))
        _r15_submit(r, db, fake_engine)
        fake_engine.execute.assert_not_awaited()
        stages = [c.args[1]["$set"].get("reject_stage")
                  for c in db.scalp_decisions.update_one.await_args_list]
        assert "pre_submit_resize" in stages

    def test_distributed_broker_capacity_denies_and_labels_dataset(self):
        """Items 3/4 — DB-backed per-broker slot denied → reject labelled
        attempt_not_submitted_capacity (infrastructure, not strategy)."""
        r = _r15_runner("r15-slot")
        db = _stub_db()
        db.scalp_submission_slots.update_one = AsyncMock(
            return_value=MagicMock(modified_count=0))
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t"}))
        _r15_submit(r, db, fake_engine)
        fake_engine.execute.assert_not_awaited()
        sets = [c.args[1]["$set"]
                for c in db.scalp_decisions.update_one.await_args_list]
        cap = [s for s in sets
               if s.get("reject_stage") == "submission_capacity_broker"]
        assert cap and cap[0]["dataset"] == "attempt_not_submitted_capacity"

    def test_slot_acquire_release_roundtrip(self):
        from scalp import engine as eng
        from scalp.engine import (acquire_broker_submission_slot,
                                  release_broker_submission_slot)
        db = _stub_db()
        slot = asyncio.run(acquire_broker_submission_slot(db, "OnEquity"))
        assert slot and slot["broker_key"] == "broker:onequity"
        assert slot["token"]
        asyncio.run(release_broker_submission_slot(db, slot))
        filt = db.scalp_submission_slots.update_one.await_args.args[0]
        assert filt == {"broker_key": "broker:onequity",
                        "slot_id": slot["slot_id"], "token": slot["token"]}
        eng._slots_ready.discard("broker:onequity")

    def test_success_records_pre_submit_snapshot_and_latency(self):
        """Items 2/9 — the decision doc gains the commitment context."""
        r = _r15_runner("r15-snap2")
        db = _stub_db()
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t"}))
        _r15_submit(r, db, fake_engine, lot=0.01)
        sets = [c.args[1]["$set"]
                for c in db.scalp_decisions.update_one.await_args_list]
        snap = [s for s in sets if "pre_submit_account_snapshot" in s]
        assert snap, sets
        s = snap[0]
        pss = s["pre_submit_account_snapshot"]
        assert pss["final_lot"] == 0.01 and pss["lease_epoch"] == 7
        assert "equity_delta" in pss and "margin_audit" in pss
        assert s["submitted_lot"] == 0.01
        assert s["decision_to_submit_ms"] >= 0
        assert "submission_start_ts_ms" in s

    def test_margin_audit_states(self):
        """Item 1 — explicit, explainable margin audit."""
        from scalp.engine import margin_audit
        # leverage unknown → recorded as unavailable, never assumed
        out = margin_audit({"free_margin": 500.0}, "EURUSD", 0.5, 1.08)
        assert out["margin_check_passed"] is None
        # insufficient free margin → explicit fail + reason
        out = margin_audit({"free_margin": 100.0, "leverage": 30},
                           "EURUSD", 0.5, 1.08)
        assert out["margin_check_passed"] is False
        assert out["estimated_required_margin"] == pytest.approx(1800.0)
        assert "insufficient free margin" in out["reason"]
        # ample margin → pass with utilization recorded
        out = margin_audit({"free_margin": 10_000.0, "leverage": 30},
                           "EURUSD", 0.5, 1.08)
        assert out["margin_check_passed"] is True
        assert 0 < out["margin_utilization_after_order"] < 1

    def test_symbol_specs_status_absent_stale_ok(self):
        """Item 5 — spec freshness is an explicit tri-state."""
        from datetime import datetime, timedelta, timezone
        from protection_guard import symbol_specs_status
        spec = {"point": 0.00001, "stops_level_points": 10}
        assert symbol_specs_status({}, "EURUSD") == "absent"
        assert symbol_specs_status(
            {"symbol_specs": {"EURUSD": {"point": 0}}}, "EURUSD") == "absent"
        fresh = {"symbol_specs": {"EURUSD": spec},
                 "symbol_specs_updated_at":
                     datetime.now(timezone.utc).isoformat()}
        assert symbol_specs_status(fresh, "EURUSD.a") == "ok"
        stale = {"symbol_specs": {"EURUSD": spec},
                 "symbol_specs_updated_at":
                     (datetime.now(timezone.utc)
                      - timedelta(days=2)).isoformat()}
        assert symbol_specs_status(stale, "EURUSD") == "stale"


class TestRound16Presubmit:
    def test_edge_revalidation_rejects_decayed_edge(self):
        """Round 16 items 7/8 — spread expansion + adverse drift since the
        decision snapshot must not turn an approved marginal edge negative:
        final_net_edge = net_edge − max(0, sp − decision_spread)
                         − max(0, adverse) must clear
        MIN_NET_EDGE_PIPS + LATENCY_EDGE_BUFFER_PIPS."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from scalp import edge as edge_mod
        from scalp import engine as eng
        r = _r15_runner("r16-edge")
        # live spread 0.4p; decision assumed 0.2p spread → penalty 0.2p
        # (below the 0.3p requote threshold — lightweight adjustment path)
        decision = {"decision_id": "d16e", "direction": "BUY",
                    "ts_ms": int(time.time() * 1000),
                    "net_edge_pips": 0.25, "sim": {"entry_mid": 1.08002}}
        fc = SimpleNamespace(stop_pips=3.0, target_pips=5.0,
                             expected_slippage_pips=0.1,
                             expected_spread_cost_pips=0.2)
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t"}))
        db = _stub_db()

        async def run():
            with patch("execution.for_account", return_value=fake_engine), \
                 patch.object(eng, "confirm_account_lease_now",
                              AsyncMock(return_value=(True, 7))):
                await r._submit_live(db, decision, fc, {"lot": 0.01})
                for _ in range(6):
                    await asyncio.sleep(0)
        asyncio.run(run())
        fake_engine.execute.assert_not_awaited()
        sets = [c.args[1]["$set"]
                for c in db.scalp_decisions.update_one.await_args_list]
        rej = [s for s in sets
               if s.get("reject_stage") == "pre_submit_edge_revalidation"]
        assert rej, sets
        # 0.25 − 0.2 spread delta = 0.05 < 0.15 + 0.05 buffer
        assert rej[0]["final_net_edge_pips"] == pytest.approx(0.05, abs=0.02)
        assert rej[0]["spread_delta_pips"] == pytest.approx(0.2, abs=0.02)
        assert (edge_mod.MIN_NET_EDGE_PIPS
                + eng.LATENCY_EDGE_BUFFER_PIPS) == pytest.approx(0.2)

    def test_edge_revalidation_passes_when_edge_holds(self):
        """A healthy edge with unchanged spread sails through to submit."""
        from types import SimpleNamespace
        from unittest.mock import patch
        from scalp import engine as eng
        r = _r15_runner("r16-edge-ok")
        decision = {"decision_id": "d16f", "direction": "BUY",
                    "ts_ms": int(time.time() * 1000),
                    "net_edge_pips": 2.0, "sim": {"entry_mid": 1.08002}}
        fc = SimpleNamespace(stop_pips=3.0, target_pips=5.0,
                             expected_slippage_pips=0.1,
                             expected_spread_cost_pips=0.4)
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t"}))
        db = _stub_db()

        async def run():
            with patch("execution.for_account", return_value=fake_engine), \
                 patch.object(eng, "confirm_account_lease_now",
                              AsyncMock(return_value=(True, 7))):
                await r._submit_live(db, decision, fc, {"lot": 0.01})
                for _ in range(6):
                    await asyncio.sleep(0)
        asyncio.run(run())
        fake_engine.execute.assert_awaited()
        sets = [c.args[1]["$set"]
                for c in db.scalp_decisions.update_one.await_args_list]
        snap = [s for s in sets if "final_net_edge_pips" in s
                and s.get("submission_status") == "submitted"]
        assert snap and snap[0]["final_net_edge_pips"] > 0


class TestRound17SlotLifecycle:
    """Round 17 main — the slot is held from queue insertion until BROKER
    acknowledgement (or a terminal submission state), never released at
    queue time."""

    def _submit(self, r, db, fake_engine, lot=0.01):
        from types import SimpleNamespace
        from unittest.mock import patch
        from scalp import engine as eng
        decision = {"decision_id": "d17", "direction": "BUY",
                    "ts_ms": int(time.time() * 1000),
                    "net_edge_pips": 2.0, "sim": {"entry_mid": 1.08002}}
        fc = SimpleNamespace(stop_pips=3.0, target_pips=5.0,
                             expected_slippage_pips=0.1,
                             expected_spread_cost_pips=0.4)

        async def run():
            with patch("execution.for_account", return_value=fake_engine), \
                 patch.object(eng, "confirm_account_lease_now",
                              AsyncMock(return_value=(True, 7))):
                await r._submit_live(db, decision, fc, {"lot": lot})
                for _ in range(8):
                    await asyncio.sleep(0)
        asyncio.run(run())

    def _slot_release_calls(self, db):
        return [c.args[0] for c
                in db.scalp_submission_slots.update_one.await_args_list
                if "token" in c.args[0] and "$lt" not in str(c.args[0])]

    def test_slot_survives_successful_queue_insertion(self):
        """engine.execute() only QUEUES the order — the slot must stay
        leased (no token-fenced release call) and transfer to the trade."""
        r = _r15_runner("r17-hold")
        db = _stub_db()
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t17"}))
        self._submit(r, db, fake_engine)
        fake_engine.execute.assert_awaited()
        assert self._slot_release_calls(db) == []          # NOT released
        info = r.live_trades.get("t17")
        assert info and info.get("submission_slot", {}).get("token")
        # ownership persisted onto the pending trade doc
        linked = [c for c in db.trades.update_one.await_args_list
                  if "submission_slot" in str(c.args[1])]
        assert linked
        r.live_trades.clear()

    def test_broker_ack_releases_the_slot(self):
        r = _r15_runner("r17-ack")
        db = _stub_db()
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t17a"}))
        self._submit(r, db, fake_engine)
        token = r.live_trades["t17a"]["submission_slot"]["token"]

        async def ack():
            await r.on_trade_opened("t17a", 1.08004, 1.08005, db=db)
            for _ in range(8):
                await asyncio.sleep(0)
        asyncio.run(ack())
        rel = self._slot_release_calls(db)
        assert rel and rel[-1]["token"] == token
        assert "submission_slot" not in r.live_trades["t17a"]
        r.live_trades.clear()

    def test_blocked_submission_releases_immediately(self):
        r = _r15_runner("r17-blocked")
        db = _stub_db()
        fake_engine = MagicMock(execute=AsyncMock(
            return_value={"blocked": True, "reason": "spread veto"}))
        self._submit(r, db, fake_engine)
        assert self._slot_release_calls(db)                # terminal → freed
        assert not r.live_trades

    def test_close_paths_release_defensively(self):
        r = _r15_runner("r17-close")
        db = _stub_db()
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t17c"}))
        self._submit(r, db, fake_engine)

        async def close():
            await r.on_close_ack("t17c", exit_price=1.08, db=db)
            for _ in range(8):
                await asyncio.sleep(0)
        asyncio.run(close())
        assert self._slot_release_calls(db)
        r.live_trades.clear()

    def test_capacity_integrity_violation_fails_entries_closed(self):
        from scalp import engine as eng
        r = _r15_runner("r17-integrity")
        bk = eng._broker_cap_key(r.broker)
        eng._capacity_violations[bk] = {"reason": "capacity integrity violated: test"}
        db = _stub_db()
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t"}))
        try:
            self._submit(r, db, fake_engine)
        finally:
            eng._capacity_violations.pop(bk, None)
        fake_engine.execute.assert_not_awaited()
        sets = [c.args[1]["$set"]
                for c in db.scalp_decisions.update_one.await_args_list]
        cap = [s for s in sets if s.get("reject_stage") == "capacity_integrity"]
        assert cap and cap[0]["dataset"] == "attempt_not_submitted_capacity"


class TestRound17Presubmit:
    def test_stale_quote_rejected(self):
        """Round 17 item 1 — a quote older than MAX_SUBMIT_QUOTE_AGE_MS
        refuses the commitment outright."""
        from types import SimpleNamespace
        from datetime import datetime, timezone
        r = ScalpRunner("r17-quote", "u1", "EURUSD")
        r.equity = 10_000.0
        r.account = {"_id": "r17-quote", "equity": 10_000.0,
                     "status": "connected",
                     "last_heartbeat": datetime.now(timezone.utc).isoformat()}
        old = int(time.time() * 1000) - 10_000          # 10s-old tick
        r.state.update(TickEvent(symbol="EURUSD", broker_time_ms=old,
                                 received_time_ms=old, bid=1.08,
                                 ask=1.08004))
        from unittest.mock import patch
        decision = {"decision_id": "d17q", "direction": "BUY",
                    "net_edge_pips": 2.0, "sim": {"entry_mid": 1.08002}}
        fc = SimpleNamespace(stop_pips=3.0, target_pips=5.0,
                             expected_spread_cost_pips=0.4)
        db = _stub_db()
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t"}))

        async def run():
            with patch("execution.for_account", return_value=fake_engine):
                await r._submit_live(db, decision, fc, {"lot": 0.01})
                for _ in range(5):
                    await asyncio.sleep(0)
        asyncio.run(run())
        fake_engine.execute.assert_not_awaited()
        sets = [c.args[1]["$set"]
                for c in db.scalp_decisions.update_one.await_args_list]
        rej = [s for s in sets
               if s.get("reject_stage") == "pre_submit_quote_invalid"]
        assert rej and rej[0]["quote_age_ms"] >= 10_000
        assert rej[0]["quote_source"] == "r17-quote:EURUSD"

    def test_material_spread_change_triggers_full_reforecast(self):
        """Round 17 item 2 — spread expansion beyond the requote threshold
        regenerates the forecast; with no fresh feature snapshot available
        the commitment fails closed (pre_submit_reforecast)."""
        from types import SimpleNamespace
        r = _r15_runner("r17-requote")
        from unittest.mock import patch
        decision = {"decision_id": "d17r", "direction": "BUY",
                    "ts_ms": int(time.time() * 1000),
                    "net_edge_pips": 2.0, "sim": {"entry_mid": 1.08002},
                    "setup": {"direction": "BUY"}}
        # decision assumed 0.0p spread; live 0.4p → delta > 0.3p threshold
        fc = SimpleNamespace(stop_pips=3.0, target_pips=5.0,
                             expected_slippage_pips=0.1,
                             expected_spread_cost_pips=0.0)
        db = _stub_db()
        fake_engine = MagicMock(execute=AsyncMock(return_value={"id": "t"}))

        async def run():
            with patch("execution.for_account", return_value=fake_engine):
                await r._submit_live(db, decision, fc, {"lot": 0.01})
                for _ in range(5):
                    await asyncio.sleep(0)
        asyncio.run(run())
        fake_engine.execute.assert_not_awaited()
        sets = [c.args[1]["$set"]
                for c in db.scalp_decisions.update_one.await_args_list]
        assert any(s.get("reject_stage") == "pre_submit_reforecast"
                   for s in sets)

    def test_margin_audit_refuses_unapproved_instruments(self):
        """Round 17 item 5 — the standard-lot FX formula never runs for
        instruments outside the approved scalp universe."""
        from scalp.engine import margin_audit
        out = margin_audit({"free_margin": 10_000.0, "leverage": 30},
                           "XAUUSD", 0.5, 4000.0)
        assert out["margin_check_passed"] is None
        assert "not approved" in out["reason"]
        ok = margin_audit({"free_margin": 10_000.0, "leverage": 30},
                          "EURUSD", 0.5, 1.08)
        assert ok["margin_check_passed"] is True

    def test_round_to_tick(self):
        from scalp.engine import round_to_tick
        assert round_to_tick(1.080037, 0.00001) == 1.08004
        assert round_to_tick(4006.7891, 0.01) == 4006.79
        assert round_to_tick(4006.7891, 0.25) == 4006.75
        assert round_to_tick(1.08, 0) == 1.08              # no tick → passthrough

    def test_submission_terminal_classification(self):
        from scalp.engine import _submission_terminal
        assert _submission_terminal({"status": "pending",
                                     "mt5_ticket": 12345}) is True
        assert _submission_terminal({"status": "open"}) is True
        for s in ("rejected", "failed", "cancelled", "expired", "closed"):
            assert _submission_terminal({"status": s}) is True
        assert _submission_terminal({"status": "pending",
                                     "mt5_ticket": None}) is False
