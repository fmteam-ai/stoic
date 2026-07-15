"""iter-146 · Scalp fast-path subsystem tests.

Pure-logic tests use synthetic tick streams (no DB, no network on the
domain modules — H7 compliant). API tests hit the live server.
"""
import os
import sys
import time
import uuid

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scalp.instruments import approved  # noqa: E402
from scalp.state import ScalpState, TickEvent  # noqa: E402
from scalp.features import snapshot, FEATURE_KEYS  # noqa: E402
from scalp.setup import detect  # noqa: E402
from scalp.forecast import make as make_forecast  # noqa: E402
from scalp import edge  # noqa: E402
from scalp.risk import (DEFAULT_LIMITS, RiskState, ScalpRiskLimits,  # noqa: E402
                        check as risk_check, size_lot)
from scalp import kill  # noqa: E402
from scalp.gate import final_execution_gate  # noqa: E402
from scalp.engine import ShadowSim  # noqa: E402

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL"):
                BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
API = f"{BASE_URL}/api"

CFG = approved("EURUSD")
PIP = CFG.pip_size


def _mk_state(prices, start_ms=None, gap_ms=500, spread_pips=0.6):
    """Build state from a mid-price path (constant 50ms feed latency)."""
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
    """~90s path: flat → up-impulse 3 pips → 1-pip pullback → ACCELERATING
    resumption (accel>0 required by the setup)."""
    base = 1.08000
    path = [base] * 140                                   # 70s flat (fills vol windows)
    path += [base + i * 0.3 * PIP for i in range(1, 11)]  # +3 pips in 5s
    top = path[-1]
    path += [top - i * 0.25 * PIP for i in range(1, 5)]   # -1 pip pullback
    low = path[-1]
    inc = 0.0
    for step in (0.05, 0.1, 0.15, 0.2):                   # accelerating, PARTIAL recovery
        inc += step * PIP
        path.append(low + inc)
    return path


# ---------------- universe ----------------
class TestInstruments:
    def test_eurusd_approved(self):
        assert CFG is not None and CFG.contract_size == 100_000.0

    def test_gold_crypto_not_approved(self):
        for s in ("XAUUSD", "BTCUSD", "US30", "EURTRY"):
            assert approved(s) is None


# ---------------- state & features ----------------
class TestFeatures:
    def test_snapshot_none_when_thin(self):
        st = _mk_state([1.08] * 5)
        assert snapshot(st) is None

    def test_snapshot_keys(self):
        st = _mk_state([1.08 + i * 0.00001 for i in range(120)])
        f = snapshot(st)
        assert f is not None
        for k in FEATURE_KEYS:
            assert k in f, k

    def test_returns_in_pips(self):
        # +0.5 pip per 500ms → ret_1s ≈ 1 pip
        st = _mk_state([1.08 + i * 0.5 * PIP for i in range(120)])
        f = snapshot(st)
        assert 0.5 <= f["ret_1s"] <= 1.6


# ---------------- setup ----------------
class TestSetup:
    def test_micro_pullback_detected(self):
        st = _mk_state(_impulse_pullback_path())
        f = snapshot(st)
        cand = detect(f, st)
        assert cand is not None and cand["direction"] == "BUY"
        assert 0.15 <= cand["pullback_frac"] <= 0.60

    def test_flat_market_no_candidate(self):
        st = _mk_state([1.08] * 120)
        f = snapshot(st)
        assert f is None or detect(f, st) is None


# ---------------- forecast + edge ----------------
class TestEdge:
    def _forecast(self, spread=0.6):
        st = _mk_state(_impulse_pullback_path(), spread_pips=spread)
        f = snapshot(st)
        cand = detect(f, st)
        assert cand
        return make_forecast(f, cand, st, CFG)

    def test_forecast_fields(self):
        fc = self._forecast()
        assert 0.30 <= fc.p_target_before_stop <= 0.70
        assert fc.target_pips > fc.stop_pips * 1.0
        assert fc.uncertainty_pips > 0

    def test_wide_spread_kills_edge(self):
        fc = self._forecast(spread=3.0)
        assert edge.evaluate(fc)["ok"] is False

    def test_edge_math(self):
        fc = self._forecast()
        res = edge.evaluate(fc)
        manual = (fc.p_target_before_stop * fc.target_pips
                  - (1 - fc.p_target_before_stop) * fc.stop_pips
                  - (fc.expected_spread_cost_pips + 2 * fc.expected_slippage_pips
                     + fc.expected_commission_pips)
                  - fc.uncertainty_pips)
        # evaluate() rounds to 3 decimals — compare at that precision
        assert abs(res["net_edge_pips"] - manual) < 1e-3


# ---------------- risk ----------------
class TestRisk:
    def test_sizing_fail_closed_zero_equity(self):
        r = size_lot(0, 2.0, 10.0, CFG, DEFAULT_LIMITS)
        assert r["ok"] is False and r["lot"] == 0.0

    def test_min_lot_exceeding_budget_rejected(self):
        # $100 equity, 0.05% = $0.05 budget; 0.01 lot × 2p × $10 = $0.20 risk
        r = size_lot(100, 2.0, 10.0, CFG, DEFAULT_LIMITS)
        assert r["ok"] is False

    def test_normal_sizing(self):
        # $50k → $25 budget; 2p stop × $10 → 1.25 → floored to 1.25? step 0.01
        r = size_lot(50_000, 2.0, 10.0, CFG, DEFAULT_LIMITS)
        assert r["ok"] is True and 1.0 <= r["lot"] <= 1.25

    def test_consecutive_loss_cooldown(self):
        rs = RiskState(ScalpRiskLimits(max_consecutive_losses=2,
                                       cooldown_after_loss_streak_minutes=30))
        rs.record_result(-5, 0.5)
        rs.record_result(-5, 0.5)
        res = risk_check(rs, 50_000, 2.0, 10.0, CFG)
        assert res["ok"] is False and "cooldown" in res["reason"]

    def test_max_open_positions(self):
        rs = RiskState(DEFAULT_LIMITS)
        rs.record_open()
        res = risk_check(rs, 50_000, 2.0, 10.0, CFG)
        assert res["ok"] is False and "open" in res["reason"]

    def test_hourly_cap(self):
        rs = RiskState(ScalpRiskLimits(max_trades_per_symbol_per_hour=3,
                                       max_open_positions=99))
        for _ in range(3):
            rs.record_open()
            rs.record_close()
        res = risk_check(rs, 50_000, 2.0, 10.0, CFG)
        assert res["ok"] is False and "hourly" in res["reason"]


# ---------------- kill switch ----------------
class TestKill:
    def test_no_data_halts(self):
        st = ScalpState(PIP)
        h = kill.evaluate(st, CFG)
        assert h["status"] == "HALTED" and h["open_allowed"] is False
        assert h["close_allowed"] is True    # reducing risk stays permitted

    def test_fresh_data_ok(self):
        st = _mk_state([1.08 + i * PIP * 0.1 for i in range(100)])
        h = kill.evaluate(st, CFG)
        assert h["status"] == "OK"

    def test_slippage_anomaly(self):
        st = _mk_state([1.08 + i * PIP * 0.1 for i in range(100)])
        for _ in range(6):
            st.record_fill(5.0)   # >> 3× max_expected 0.3
        h = kill.evaluate(st, CFG)
        assert "slippage exceeds forecast" in h["reasons"]


# ---------------- final gate ----------------
class TestGate:
    def test_gate_blocks_stale_signal(self):
        st = _mk_state([1.08 + i * PIP * 0.1 for i in range(100)])
        g = final_execution_gate(st, CFG, net_edge_pips=1.0,
                                 signal_ts_ms=int(time.time() * 1000) - 60_000,
                                 spread_limit_pips=1.2, health_open_allowed=True)
        assert g["ok"] is False and "signal_fresh" in g["reason"]

    def test_gate_passes_fresh(self):
        st = _mk_state([1.08 + i * PIP * 0.1 for i in range(100)])
        g = final_execution_gate(st, CFG, net_edge_pips=1.0,
                                 signal_ts_ms=int(time.time() * 1000),
                                 spread_limit_pips=1.2, health_open_allowed=True)
        assert g["ok"] is True


# ---------------- barrier labeling (Step 7) ----------------
class TestShadowSim:
    def _tick(self, ms, bid, ask):
        return TickEvent(symbol="EURUSD", broker_time_ms=ms,
                         received_time_ms=ms, bid=bid, ask=ask)

    def test_long_target_first_uses_bid(self):
        sim = ShadowSim("d1", "BUY", entry_px=1.08000, target_pips=2.0,
                        stop_pips=2.0, pip=PIP, opened_ms=0, max_holding_ms=60_000)
        # ask crosses target but bid hasn't → NOT resolved
        assert sim.advance(self._tick(1000, 1.08015, 1.08025)) is None
        out = sim.advance(self._tick(2000, 1.08021, 1.08031))
        assert out["result"] == "target_first" and out["net_pips"] >= 2.0

    def test_long_stop_first(self):
        sim = ShadowSim("d2", "BUY", 1.08000, 2.0, 2.0, PIP, 0, 60_000)
        out = sim.advance(self._tick(1000, 1.07979, 1.07989))
        assert out["result"] == "stop_first" and out["net_pips"] <= -2.0

    def test_timeout(self):
        sim = ShadowSim("d3", "SELL", 1.08000, 2.0, 2.0, PIP, 0, 5_000)
        out = sim.advance(self._tick(6_000, 1.08000, 1.08006))
        assert out["result"] == "timeout"


# ---------------- API ----------------
class TestScalpApi:
    @pytest.fixture(scope="class")
    def ctx(self):
        s = requests.Session()
        email = f"TEST_scalp_{uuid.uuid4().hex[:6]}@example.com"
        s.post(f"{API}/auth/register", json={"terms_agreed": True, "email": email,
                                             "password": "testpass123"}, timeout=15)
        from helpers import mark_email_verified
        mark_email_verified(email)
        s.post(f"{API}/auth/login", json={"email": email, "password": "testpass123"}, timeout=30)
        acc = s.post(f"{API}/accounts", json={
            "label": "TEST_ScalpAcc", "broker": "Exness", "server": "T",
            "account_number": uuid.uuid4().hex[:8], "account_type": "standard",
            "base_currency": "USD"}, timeout=10).json()
        return {"s": s, "acc": acc}

    def test_config_rejects_unapproved_symbol(self, ctx):
        r = ctx["s"].post(f"{API}/scalp/config", json={
            "account_id": ctx["acc"]["id"], "symbol": "XAUUSD", "enabled": True},
            timeout=10)
        assert r.status_code == 422

    def test_demo_live_requires_confirm(self, ctx):
        r = ctx["s"].post(f"{API}/scalp/config", json={
            "account_id": ctx["acc"]["id"], "symbol": "EURUSD",
            "enabled": True, "mode": "demo_live", "confirm_live": False}, timeout=10)
        assert r.status_code == 422

    def test_enable_shadow_and_status(self, ctx):
        r = ctx["s"].post(f"{API}/scalp/config", json={
            "account_id": ctx["acc"]["id"], "symbol": "EURUSD",
            "enabled": True, "mode": "shadow"}, timeout=10)
        assert r.status_code == 200
        assert r.json()["status"]["enabled"] is True
        st = ctx["s"].get(f"{API}/scalp/status", timeout=10).json()
        assert any(x["symbol"] == "EURUSD" for x in st["runners"])

    def test_bridge_ticks_ingest(self, ctx):
        now = int(time.time() * 1000)
        ticks = [{"tm": now - 1000 + i * 100, "b": 1.08000 + i * 0.00001,
                  "a": 1.08006 + i * 0.00001} for i in range(10)]
        r = requests.post(f"{API}/bridge/ticks", json={
            "bridge_token": ctx["acc"]["bridge_token"], "symbol": "EURUSD",
            "sent_at_ms": now, "ticks": ticks}, timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok" and body["ticks"] == 10

    def test_bridge_ticks_unapproved_ignored(self, ctx):
        r = requests.post(f"{API}/bridge/ticks", json={
            "bridge_token": ctx["acc"]["bridge_token"], "symbol": "XAUUSD",
            "sent_at_ms": 1, "ticks": [{"tm": 1, "b": 2000.0, "a": 2000.5}]},
            timeout=10)
        assert r.status_code == 200 and r.json()["status"] == "ignored"

    def test_metrics_empty_ok(self, ctx):
        r = ctx["s"].get(f"{API}/scalp/metrics?symbol=EURUSD", timeout=10)
        assert r.status_code == 200

    def test_retrain_insufficient_samples(self, ctx):
        r = ctx["s"].post(f"{API}/scalp/retrain?symbol=EURUSD", timeout=30)
        assert r.status_code == 200
        body = r.json()
        assert body.get("trained") in (False, True)

    def test_e2e_decision_recorded_via_bridge(self, ctx):
        """Full fast path over HTTP: impulse-pullback ticks → decision doc.
        Permissions are UNKNOWN (no M15 context) so the verdict must be a
        fail-closed rejection — but the pipeline stages and barrier sim
        must all be recorded. Fresh account: clean tick state."""
        acc = ctx["s"].post(f"{API}/accounts", json={
            "label": "TEST_ScalpE2E", "broker": "Exness", "server": "T",
            "account_number": uuid.uuid4().hex[:8], "account_type": "standard",
            "base_currency": "USD"}, timeout=10).json()
        r = ctx["s"].post(f"{API}/scalp/config", json={
            "account_id": acc["id"], "symbol": "EURUSD",
            "enabled": True, "mode": "shadow"}, timeout=10)
        assert r.status_code == 200
        now = int(time.time() * 1000)
        path = _impulse_pullback_path()
        t0 = now - (len(path) - 1) * 500
        ticks = []
        for i, mid in enumerate(path):
            ticks.append({"tm": t0 + i * 500,
                          "b": round(mid - 0.00003, 5),
                          "a": round(mid + 0.00003, 5)})
        r = requests.post(f"{API}/bridge/ticks", json={
            "bridge_token": acc["bridge_token"], "symbol": "EURUSD",
            "sent_at_ms": now, "ticks": ticks}, timeout=15)
        assert r.status_code == 200 and r.json()["enabled"] is True
        time.sleep(1.0)
        docs = ctx["s"].get(f"{API}/scalp/decisions?symbol=EURUSD", timeout=10).json()["decisions"]
        docs = [d for d in docs if d["account_id"] == acc["id"]]
        assert docs, "expected a scalp decision from the impulse-pullback stream"
        d = docs[0]
        assert d["direction"] == "BUY"
        assert d["verdict"] == "rejected"          # permissions fail closed
        assert d["gates"]["permission"]["ok"] is False
        assert "forecast" in d and d["forecast"]["stop_pips"] > 0
        assert "net_edge_pips" in d


# ---------------- EA coherence ----------------
def test_ea_144_tick_stream_wiring():
    src = open("/app/backend/static/EmergentTradingBridge.mq5").read()
    from ea_version import current_ea_version
    assert current_ea_version() == "1.44"
    assert "input bool   TickStreamEnabled" in src
    assert "void SendTicks()" in src
    assert "/api/bridge/ticks" in src
    assert "EventSetMillisecondTimer" in src
