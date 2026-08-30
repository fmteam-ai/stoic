"""iter-191 — v53 review batch: flatten verification (A), configurable
scaling curves (B), extended dual-auth kinds (C), held-out calibration (D)."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
ADMIN_A = ("admin@trading.bot", "admin123")
ADMIN_B = ("admin@stoicaibot.com", "admin123")
TIMEOUT = 25


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _login(creds):
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": creds[0], "password": creds[1]},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def _create_program(s, name):
    r = s.post(f"{API}/pamm/programs", json={"name": name}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()


def _cleanup(program_id):
    db = _db()
    p = _run(db.pamm_programs.find_one({"program_id": program_id})) or {}
    bpid = p.get("broker_program_id")
    for c in ("pamm_programs", "pamm_master_accounts", "pamm_allocations",
              "pamm_nav_snapshots", "pamm_reconciliation", "pamm_audit",
              "pamm_change_requests"):
        _run(getattr(db, c).delete_many({"program_id": program_id}))
    _run(db.pamm_events.delete_many({"data.program_id": program_id}))
    _run(db.pamm_notifications.delete_many({"program_id": program_id}))
    if bpid:
        for c in ("sandbox_broker_programs", "sandbox_broker_investors",
                  "sandbox_broker_allocations", "sandbox_broker_positions"):
            _run(getattr(db, c).delete_many({"program_id": bpid}))


class TestFlattenVerification:
    def test_verified_flatten_clears_and_records(self):
        s = _login(ADMIN_A)
        prog = _create_program(s, f"fv-ok-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            _run(db.sandbox_broker_positions.insert_many([
                {"position_id": f"p{i}", "program_id": p["broker_program_id"],
                 "symbol": "EURUSD", "exposure_usd": 100} for i in range(3)]))
            r = s.post(f"{API}/pamm/programs/{pid}/emergency-stop",
                       json={"reason": "test"}, timeout=TIMEOUT)
            assert r.status_code == 200
            p2 = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert "flatten_failed" not in p2  # broker CONFIRMED flat
            ev = _run(db.pamm_events.find_one(
                {"type": "PositionsFlattened", "data.program_id": pid}))
            assert ev["data"]["verified_flat"] is True
            assert ev["data"]["closed"] == 3
        finally:
            _cleanup(pid)

    def test_failed_flatten_is_critical_incident_and_sweep_retries(self):
        """Broker unreachable during flatten → FLATTEN_FAILED critical
        incident; the sweep retries and resolves once broker is back."""
        s = _login(ADMIN_A)
        prog = _create_program(s, f"fv-fail-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            # break the adapter by pointing the program at a dead partner
            _run(db.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"partner_id": "prt_dead_xyz"}}))
            r = s.post(f"{API}/pamm/programs/{pid}/emergency-stop",
                       json={"reason": "test"}, timeout=TIMEOUT)
            assert r.status_code == 200  # state still escalates
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert p["op_state"] == "emergency_flatten"
            assert p["flatten_failed"]["attempts"] == 1
            assert p["flatten_failed"]["error"]
            ev = _run(db.pamm_events.find_one(
                {"type": "FlattenFailed", "data.program_id": pid}))
            assert ev is not None
            # escalation ladder: attempt 1 = retry only, NO alert yet
            note = _run(db.pamm_notifications.find_one(
                {"type": "FlattenFailed", "program_id": pid}))
            assert note is None
            # heal the broker; sweep retries and verifies flat
            _run(db.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"partner_id": "prt_sandbox"}}))
            from modules.pamm.sweep import sweep_once
            res = _run(sweep_once(db))
            assert res["flatten_retries"] >= 1
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert "flatten_failed" not in p
            ev = _run(db.pamm_events.find_one(
                {"type": "FlattenResolved", "data.program_id": pid}))
            assert ev is not None
        finally:
            _cleanup(pid)


class TestScalingCurves:
    def test_curve_shapes(self):
        from modules.pamm.risk.verdict import curve_factor
        # linear: full below 50%, half at 75%, 0 at cap
        assert curve_factor(2.0, 5.0, "linear") == 1.0
        assert curve_factor(3.75, 5.0, "linear") == 0.5
        assert curve_factor(5.0, 5.0, "linear") == 0.0
        # exponential: steeper — 0.25 at 75% utilization
        assert curve_factor(3.75, 5.0, "exponential") == 0.25
        # step tiers
        assert curve_factor(2.0, 5.0, "step") == 1.0
        assert curve_factor(3.0, 5.0, "step") == 0.5
        assert curve_factor(4.0, 5.0, "step") == 0.25
        assert curve_factor(4.8, 5.0, "step") == 0.0
        # logistic: ~1 low, 0.5 at 75%, ~0 near cap
        assert curve_factor(1.0, 5.0, "logistic") > 0.95
        assert abs(curve_factor(3.75, 5.0, "logistic") - 0.5) < 0.01
        assert curve_factor(4.9, 5.0, "logistic") < 0.1
        # hard: boolean
        assert curve_factor(4.99, 5.0, "hard") == 1.0
        assert curve_factor(5.0, 5.0, "hard") == 0.0

    def test_curve_configurable_via_api(self):
        s = _login(ADMIN_A)
        prog = _create_program(s, f"cv-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            # defaults: drawdown=exponential, exposure=logistic, corr=step
            r = s.get(f"{API}/pamm/programs/{pid}/risk-limits",
                      timeout=TIMEOUT)
            lim = r.json()["risk_limits"]
            assert lim["max_drawdown_pct"]["curve"] == "exponential"
            assert lim["max_exposure_pct"]["curve"] == "logistic"
            assert lim["max_correlated_positions"]["curve"] == "step"
            # change a curve (not loosening → applies instantly)
            r = s.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"daily_loss_pct": {"curve": "exponential"}},
                      timeout=TIMEOUT)
            assert r.status_code == 200 and not r.json().get(
                "pending_approval")
            # invalid curve rejected
            r = s.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"daily_loss_pct": {"curve": "banana"}},
                      timeout=TIMEOUT)
            assert r.status_code == 400
            # verdict now uses the exponential curve: 75% used → ×0.25
            now = datetime.now(timezone.utc)
            _run(db.pamm_nav_snapshots.insert_many([
                {"program_id": pid, "nav": 100000.0, "currency": "USD",
                 "at": (now - timedelta(days=1)).isoformat()},
                {"program_id": pid, "nav": 96250.0, "currency": "USD",
                 "at": now.isoformat()}]))
            _run(db.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"last_nav": {"nav": 96250.0,
                                       "at": now.isoformat()}}}))
            r = s.post(f"{API}/pamm/programs/{pid}/trade-verdict",
                       json={"requested_risk_pct": 0.40}, timeout=TIMEOUT)
            body = r.json()
            assert body["verdict"] == "REDUCE"
            assert body["approved_risk_pct"] == 0.10  # 0.40 × 0.25
            daily = next(f for f in body["factors"]
                         if f["limit"] == "daily_loss_pct")
            assert daily["curve"] == "exponential"
        finally:
            _cleanup(pid)


class TestExtendedDualAuth:
    def test_new_kinds_accepted_and_applied(self):
        a = _login(ADMIN_A)
        b = _login(ADMIN_B)
        prog = _create_program(a, f"dk-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            r = a.post(f"{API}/pamm/programs/{pid}/change-requests",
                       json={"kind": "fee_change",
                             "payload": {"manager_fee_pct": 25.0},
                             "reason": "new terms"}, timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            cid = r.json()["change_id"]
            r = b.post(f"{API}/pamm/change-requests/{cid}/approve",
                       timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert p["governance"]["fee_change"]["payload"][
                "manager_fee_pct"] == 25.0
            # unknown kind rejected
            r = a.post(f"{API}/pamm/programs/{pid}/change-requests",
                       json={"kind": "make_me_rich"}, timeout=TIMEOUT)
            assert r.status_code == 400
        finally:
            _cleanup(pid)

    def test_all_review_kinds_registered(self):
        from modules.pamm.dualauth import CRITICAL_KINDS
        for k in ("strategy_replacement", "strategy_version_promotion",
                  "leverage_cap_increase", "max_aum_increase", "fee_change",
                  "withdrawal_rules_change", "allocation_method_change",
                  "risk_limits_increase", "unlock"):
            assert k in CRITICAL_KINDS


class TestHeldOutCalibration:
    def test_holdout_indices_threshold(self):
        from probability_calibrator import holdout_tail_indices
        assert holdout_tail_indices(99) is None
        idx = holdout_tail_indices(100)
        assert idx is not None and idx[0] == 70 and idx[-1] == 99

    def test_ece_metric(self):
        from probability_calibrator import expected_calibration_error
        rng = np.random.default_rng(7)
        # perfectly calibrated: label drawn with prob = score
        scores = rng.uniform(0.05, 0.95, 4000)
        labels = (rng.uniform(size=4000) < scores).astype(float)
        assert expected_calibration_error(scores, labels) < 0.05
        # badly calibrated: constant 0.9 prediction on 50/50 outcomes
        bad = np.full(1000, 0.9)
        labels50 = (rng.uniform(size=1000) < 0.5).astype(float)
        assert expected_calibration_error(bad, labels50) > 0.3

    def test_fit_artifact_uses_holdout_tail(self):
        """When walk-forward is degenerate, n>=100 uses the chronological
        tail (never in-sample)."""
        import learned_meta as lm
        rng = np.random.default_rng(3)
        n = 120
        X = rng.normal(size=(n, len(lm_feature_count())))
        signal = X[:, 0] * 1.5 + rng.normal(scale=0.8, size=n)
        y = (signal > 0).astype(int)
        doc = lm._fit_artifact(X, y.astype(float), "test_key", "TEST")
        assert doc["calibration_source"] in ("walk_forward_oos",
                                             "holdout_tail")
        cal = doc["calibration"]
        assert "ece_raw" in cal and "ece_calibrated" in cal
        assert cal["n"] < n  # calibrated on a SUBSET, never all samples

    def test_calibration_history_recorded(self):
        import learned_meta as lm
        db = _db()
        doc = {"key": "test_cal_hist", "trained_at": "2026-06-01T00:00:00",
               "calibration_source": "holdout_tail", "oos_auc": 0.61,
               "calibration": {"n": 36, "brier_raw": 0.24,
                               "brier_calibrated": 0.22, "ece_raw": 0.09,
                               "ece_calibrated": 0.05, "skipped": False}}
        _run(lm._record_calibration_history(db, doc))
        try:
            h = _run(db.calibration_history.find_one(
                {"key": "test_cal_hist"}))
            assert h["ece_calibrated"] == 0.05
            assert h["source"] == "holdout_tail"
        finally:
            _run(db.calibration_history.delete_many(
                {"key": "test_cal_hist"}))


def lm_feature_count():
    return ["confidence_norm", "is_buy", "kalman_vel_norm", "cot_against",
            "cot_with", "tips_aligned", "mtf_aligned", "macro_event_24h"]


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
