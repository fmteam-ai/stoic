"""Iteration 105 live HTTP checks — Shadow Readiness endpoints via preview URL.

Covers: /shadow/health, /shadow/benchmark, /shadow/validation, /twin/stress,
and the promotion gate BLOCK path (attempts autonomous_live promotion which
should be rejected while shadow health is below threshold).

SAFETY: never actually promote config; only assert the block. Admin config
must remain supervised_live.
"""
import os
import pytest
import requests


BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open("/app/frontend/.env") as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL="):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


def _read_env(key):
    try:
        with open("/app/backend/.env") as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        return None
    return None


STEP_UP_BYPASS_TOKEN = _read_env("STEP_UP_BYPASS_TOKEN")


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    assert r.status_code == 200, f"login failed {r.status_code} {r.text}"
    return s


def _csrf_headers(session):
    csrf = session.cookies.get("csrf_token")
    h = {}
    if csrf:
        h["X-CSRF-Token"] = csrf
    if STEP_UP_BYPASS_TOKEN:
        h["X-Step-Up-Bypass"] = STEP_UP_BYPASS_TOKEN
    return h


# ─── /api/shadow/health ─────────────────────────────────────────
def test_shadow_health_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/shadow/health", timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "components" in d and "overall" in d
    assert d.get("threshold") == 60
    assert "promotions_paused" in d
    expected = {"data_freshness", "regime_confidence", "calibration_quality",
                "execution_quality", "broker_stability", "worker_health",
                "synchronization"}
    assert set(d["components"].keys()) == expected, f"got {set(d['components'].keys())}"
    # If overall is numeric, verify pause flag consistency (fail-closed:
    # missing critical data also pauses promotions regardless of score)
    if isinstance(d["overall"], (int, float)):
        expected = d["overall"] < 60 or bool(d.get("fail_closed"))
        assert d["promotions_paused"] == expected


# ─── /api/shadow/benchmark ──────────────────────────────────────
def test_shadow_benchmark_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/shadow/benchmark?days=30", timeout=45)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "variants" in d
    variants = d["variants"]
    for k in ("current_production", "previous_production",
              "experimental_ai", "rule_baseline"):
        assert k in variants, f"missing variant {k}"
    assert "decision_quality" in d
    q = d["decision_quality"]
    for k in ("false_positive_rate", "false_negative_rate",
              "executed", "intercepted_replayed"):
        assert k in q, f"missing decision_quality.{k}"
    assert "definitions" in d


def test_shadow_benchmark_days_clamp(admin_session):
    # FastAPI Query enforces range 7-90 via 422 validation for out-of-range
    r_small = admin_session.get(f"{BASE_URL}/api/shadow/benchmark?days=1", timeout=45)
    r_large = admin_session.get(f"{BASE_URL}/api/shadow/benchmark?days=999", timeout=45)
    assert r_small.status_code == 422, f"expected 422 for days=1, got {r_small.status_code}"
    assert r_large.status_code == 422, f"expected 422 for days=999, got {r_large.status_code}"
    # Boundaries accepted
    r_min = admin_session.get(f"{BASE_URL}/api/shadow/benchmark?days=7", timeout=45)
    r_max = admin_session.get(f"{BASE_URL}/api/shadow/benchmark?days=90", timeout=45)
    assert r_min.status_code == 200 and r_max.status_code == 200


# ─── /api/twin/stress ───────────────────────────────────────────
def test_twin_stress_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/twin/stress?days=30", timeout=45)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "decisions" in d and "clean_net_r" in d
    assert "scenarios" in d and isinstance(d["scenarios"], list)
    names = {sc["scenario"] for sc in d["scenarios"]}
    expected = {"latency_spike", "delayed_fill", "spread_explosion",
                "liquidity_drop", "broker_outage", "market_gap"}
    assert names == expected, f"got {names}"
    for sc in d["scenarios"]:
        assert "net_r" in sc and "delta_r" in sc and "verdict" in sc
        assert sc["verdict"] in ("RESILIENT", "DEGRADED")
    assert "worst_case" in d


# ─── /api/shadow/validation ─────────────────────────────────────
def test_shadow_validation_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/shadow/validation", timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    for k in ("total", "agreed", "agreement_rate", "dissent_by_verdict", "recent"):
        assert k in d, f"missing key {k}"


# ─── Promotion gate BLOCK path ──────────────────────────────────
def test_promotion_gate_blocks_when_health_low(admin_session):
    """Attempt to raise operational_mode to autonomous_live; must be blocked
    with a shadow-health reason. Config must remain unchanged."""
    # First read current config
    r0 = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=20)
    assert r0.status_code == 200
    before = r0.json()
    mode_before = before.get("operational_mode") or (before.get("config") or {}).get("operational_mode")

    h = _csrf_headers(admin_session)
    # Try promotion — expect either 4xx block OR 200 with blockers/no change
    r = admin_session.put(
        f"{BASE_URL}/api/bot/config",
        json={"operational_mode": "autonomous_live"},
        headers=h, timeout=30)
    body_text = r.text.lower()
    blocked = (r.status_code >= 400) or ("shadow" in body_text and (
        "paus" in body_text or "block" in body_text or "< 60" in body_text
        or "health" in body_text))
    # Also verify config wasn't actually raised
    r2 = admin_session.get(f"{BASE_URL}/api/bot/config", timeout=20)
    after = r2.json()
    mode_after = after.get("operational_mode") or (after.get("config") or {}).get("operational_mode")
    assert mode_after != "autonomous_live", f"SAFETY: mode was raised! {mode_after}"
    assert mode_after == mode_before, f"config changed: {mode_before} -> {mode_after}"
    assert blocked, (f"Expected block but got status={r.status_code}, "
                     f"body first 300 chars: {r.text[:300]}")
