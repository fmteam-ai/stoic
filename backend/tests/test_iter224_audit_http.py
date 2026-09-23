from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-224 · HTTP-level verification of P1-1..P1-5 audit changes.

Complements iter-204 unit-level acceptance tests by exercising the public HTTP
surface via REACT_APP_BACKEND_URL (what the browser actually sees).
"""
import os
import uuid
import requests
import pytest

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
METRICS_TOKEN = os.environ.get("METRICS_TOKEN") or open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")).read().split("METRICS_TOKEN=")[1].split()[0]


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": "admin@stoicaibot.com", "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, r.text
    return s


# ── P1-1 & P1-2 : public trust-stats ─────────────────────────────────────────
def test_public_trust_stats_shape():
    r = requests.get(f"{BASE}/api/public/trust-stats", timeout=15)
    assert r.status_code == 200, r.text
    p = r.json()
    # P1-2 versioned populations
    assert p["population_version"] == "trust-stats/v2"
    # verified_active_accounts
    v = p["verified_active_accounts"]
    for k in ("count", "environment", "definition", "exclusions"):
        assert k in v, f"missing {k} in verified_active_accounts"
    for env in ("LIVE", "DEMO", "PAPER"):
        assert env in v["environment"]
    # execution_intents_blocked
    b = p["execution_intents_blocked"]
    for k in ("count", "period_days", "definition", "exclusions"):
        assert k in b
    assert b["period_days"] == 30
    # P1-1 availability — value_pct null until reconciled 30d window
    a = p["availability"]
    assert "sli" in a and "value_pct" in a and "status" in a and "reconciled" in a
    assert a["value_pct"] is None
    assert a["reconciled"] is False
    assert a["status"] in ("no_probes", "window_incomplete", "no_regions_configured", "no_one_minute_prober_configured")
    # ops monitoring coverage
    assert "ops_monitoring_coverage_pct" in p
    assert "ops_monitoring_coverage_definition" in p
    # legacy keys still present but uptime null
    assert "accounts_protected" in p and "signals_vetoed" in p
    assert p["uptime_30d_pct"] is None
    # meta
    for k in ("period", "as_of", "reconciliation", "legal_review", "ttl_seconds"):
        assert k in p, f"missing meta key {k}"


# ── P1-1 : edge-probe requires token, always 401 in preview (EDGE_PROBE_TOKEN unset) ─
def test_edge_probe_missing_token_401():
    r = requests.post(f"{BASE}/api/public/edge-probe", json={}, timeout=10)
    assert r.status_code == 401


def test_edge_probe_wrong_token_401():
    r = requests.post(f"{BASE}/api/public/edge-probe",
                      headers={"X-Edge-Probe-Token": "anything-since-unset"},
                      json={"region": "test"}, timeout=10)
    assert r.status_code == 401


# ── P1-3/P1-4 : repair-ledger verify endpoint ────────────────────────────────
def test_repair_ledger_verify_admin(admin_session):
    r = admin_session.get(f"{BASE}/api/admin/repair-ledger/verify", timeout=20)
    assert r.status_code == 200, r.text
    p = r.json()
    for k in ("ok", "chained_entries", "legacy_unchained_entries",
              "pending_incomplete", "anomalies", "ledger_class", "verified_at"):
        assert k in p, f"missing {k}"
    assert "append-only" in p["ledger_class"].lower()
    assert isinstance(p["anomalies"], list)


def test_repair_ledger_verify_non_admin_forbidden():
    s = requests.Session()
    email = f"nonadm_{uuid.uuid4().hex[:8]}@example.com"
    reg = s.post(f"{BASE}/api/auth/register",
                 json={"email": email, "password": "Kd5#Zt9mW2xVpR7c",
                       "name": "Test", "terms_agreed": True}, timeout=15)
    assert reg.status_code in (200, 201), reg.text
    # flip email_verified via mongo
    from pymongo import MongoClient
    db = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    db.users.update_one({"email": email}, {"$set": {"email_verified": True}})
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": email, "password": "Kd5#Zt9mW2xVpR7c"}, timeout=15)
    assert r.status_code == 200, r.text
    r = s.get(f"{BASE}/api/admin/repair-ledger/verify", timeout=15)
    assert r.status_code == 403
    db.users.delete_one({"email": email})


def test_repair_ledger_list_still_200(admin_session):
    r = admin_session.get(f"{BASE}/api/admin/repair-ledger", timeout=20)
    assert r.status_code == 200, r.text
    # rows might have new fields for chained entries; endpoint just must not 500
    body = r.json()
    assert isinstance(body, dict) or isinstance(body, list)


# ── P1-5 : release-readiness exposes release_attestation ─────────────────────
def test_release_readiness_release_attestation():
    r = requests.get(f"{BASE}/api/ops/release-readiness",
                     headers={"X-Metrics-Token": METRICS_TOKEN}, timeout=15)
    # In preview many worker checks fail → endpoint returns 503, but body still populated
    assert r.status_code in (200, 503), r.text
    p = r.json()
    checks = p.get("checks", {})
    ra = checks.get("release_attestation")
    assert ra is not None, f"release_attestation missing; got: {list(checks)}"
    for k in ("enforced", "attestation_present", "running_build_sha", "ok", "note"):
        assert k in ra, f"release_attestation missing key {k}"
    # in preview: not production → ok should be True, attestation_present False
    assert ra["attestation_present"] is False
    assert ra["ok"] is True
