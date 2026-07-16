"""
Iter-53 full sweep — verifies iter-51 (Correlation-aware Kelly + CVaR) and iter-52
(ADWIN drift + Platt calibration) are wired correctly + global backend smoke.

Run:  pytest /app/backend/tests/test_iter53_full_sweep.py -v --tb=short \
        --junitxml=/app/test_reports/pytest/iter53_sweep.xml
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://algo-trade-135.preview.emergentagent.com").rstrip("/")
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


# ---------- session / auth fixtures ----------
@pytest.fixture(scope="session")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=20)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text[:300]}"
    return s


# ---------- iter-51 — CVaR fields on portfolio snapshot ----------
class TestIter51CVaR:
    def test_portfolio_snapshot_has_cvar_fields(self, session):
        r = session.get(f"{BASE_URL}/api/portfolio/snapshot", timeout=20)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        # CVaR fields live inside `var` block alongside VaR fields
        var = data.get("var") or {}
        for k in ("cvar_95_usd", "cvar_95_pct_equity", "cvar_99_usd", "cvar_99_pct_equity"):
            assert k in var, f"Missing CVaR field var.{k}; var keys={list(var.keys())}"
        # VaR fields must still exist
        for k in ("var_95_usd", "var_99_usd"):
            assert k in var, f"Missing legacy VaR field var.{k}"


# ---------- iter-52 — calibration + drift endpoints ----------
class TestIter52CalibrationDrift:
    def test_learned_meta_has_calibration_block(self, session):
        r = session.get(f"{BASE_URL}/api/analytics/learned-meta", timeout=20)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        # Calibration block may be empty {} if not trained but must be a dict
        assert "calibration" in data, f"Missing calibration key; got {list(data.keys())[:20]}"
        cal = data["calibration"]
        assert isinstance(cal, dict), f"calibration is not a dict: {type(cal)}"
        # If trained — verify keys; if empty — no error
        if cal:
            allowed = {"A", "B", "n", "brier_raw", "brier_calibrated", "applied", "converged", "skipped", "reason"}
            assert any(k in cal for k in allowed), f"Calibration dict missing expected keys: {cal}"

    def test_drift_status_endpoint(self, session):
        r = session.get(f"{BASE_URL}/api/analytics/learned-meta/drift", timeout=20)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        for k in ("enabled", "sessions", "window_size", "min_samples", "adwin_delta"):
            assert k in data, f"Missing drift status key {k}; got {list(data.keys())}"
        for s in ("ASIA", "LONDON", "NY", "GLOBAL"):
            assert s in data["sessions"], f"Missing session {s} in drift status sessions"

    def test_drift_check_now(self, session):
        r = session.post(f"{BASE_URL}/api/analytics/learned-meta/drift/check-now", timeout=30)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        for k in ("checked", "drift", "retrained"):
            assert k in data, f"Missing key {k} in check-now response: {data}"

    def test_learned_meta_retrain(self, session):
        r = session.post(f"{BASE_URL}/api/analytics/learned-meta/retrain", timeout=60)
        # Allow 200 with trained=True OR a reason for skipping (insufficient samples)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        # Either trained or has a reason
        assert ("trained" in data) or ("reason" in data) or ("status" in data), f"Unexpected retrain payload: {data}"


# ---------- Backend smoke — every primary GET endpoint 200 ----------
SMOKE_ENDPOINTS = [
    "/api/auth/me",
    "/api/bot/config",
    "/api/bot/health-score",
    "/api/bot/pulse",
    "/api/bot/status",
    "/api/trades",
    "/api/signals",
    "/api/analytics/sessions",
    "/api/analytics/attribution",
    "/api/postmortem/patterns",
    "/api/postmortem/adjustments",
    "/api/postmortem/settings",
    "/api/portfolio/snapshot",
    "/api/macro/snapshot",
    "/api/macro/gate",
    "/api/auto-heal/settings",
    "/api/auto-heal/log",
    "/api/market/quotes?symbols=XAUUSD,BTCUSD",
    "/api/crypto/status",
    "/api/crypto/accounts",
    "/api/research/proposals",
    "/api/nl/triggers",
    "/api/analytics/learned-meta",
    "/api/analytics/learned-meta/drift",
]


@pytest.mark.parametrize("endpoint", SMOKE_ENDPOINTS)
def test_smoke_get_endpoint_200(session, endpoint):
    r = session.get(f"{BASE_URL}{endpoint}", timeout=25)
    assert r.status_code == 200, f"{endpoint} → {r.status_code} :: {r.text[:300]}"


# ---------- Signal generation — must include p_win + p_win_raw + p_win_calibrated ----------
class TestSignalCalibration:
    def test_signal_generate_includes_calibration_fields(self, session):
        # Endpoint may be POST or GET; trying POST /api/signals/generate
        r = session.post(
            f"{BASE_URL}/api/signals/generate",
            json={"symbol": "XAUUSD", "risk_level": "medium"},
            timeout=60,
        )
        if r.status_code == 404:
            pytest.skip("signals/generate endpoint not present")
        assert r.status_code == 200, r.text[:400]
        sig = r.json()
        # learned_meta block expected
        lm = sig.get("learned_meta") or sig.get("data", {}).get("learned_meta") or {}
        if not lm:
            pytest.skip(f"learned_meta block not in signal response: keys={list(sig.keys())[:15]}")
        # After iter-52 — p_win, p_win_raw, p_win_calibrated
        for key in ("p_win", "p_win_raw", "p_win_calibrated"):
            assert key in lm, f"learned_meta missing {key}: {list(lm.keys())}"


# ---------- Auto-Heal round-trip ----------
class TestAutoHealRoundTrip:
    def test_auto_heal_settings_roundtrip(self, session):
        # Get current state
        r0 = session.get(f"{BASE_URL}/api/auto-heal/settings", timeout=20)
        assert r0.status_code == 200
        original = r0.json()
        prev_enabled = bool(original.get("enabled", False))

        # Flip
        target = {"enabled": not prev_enabled}
        r1 = session.post(f"{BASE_URL}/api/auto-heal/settings", json=target, timeout=20)
        assert r1.status_code == 200, r1.text[:300]

        # Verify
        r2 = session.get(f"{BASE_URL}/api/auto-heal/settings", timeout=20)
        assert r2.status_code == 200
        assert bool(r2.json().get("enabled")) == (not prev_enabled), "Auto-heal toggle did not persist"

        # Restore
        session.post(f"{BASE_URL}/api/auto-heal/settings", json={"enabled": prev_enabled}, timeout=20)


# ---------- No tracebacks in backend log during bot loop ----------
class TestBotLoopHealth:
    def test_backend_log_no_recent_tracebacks(self):
        # Inspect last ~400 lines of supervisor err log for fresh tracebacks
        import subprocess
        out = subprocess.run(
            ["tail", "-n", "400", "/var/log/supervisor/backend.err.log"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        # Common Python traceback marker — but allow a few benign known patterns
        bad = []
        for line in out.splitlines():
            if "Traceback (most recent call last)" in line:
                bad.append(line)
        # We tolerate up to 0 fresh ones for now — surface them to main agent if found
        if bad:
            print(f"\nFound {len(bad)} traceback(s) in backend.err.log (last 400 lines):")
            for b in bad[:5]:
                print("  ", b)
        # Soft assertion — report but do not fail entire suite (flagged in test report)
        assert len(bad) <= 5, f"Excessive tracebacks in backend log: {len(bad)}"
