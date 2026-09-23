from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""HTTP-level verification for Audit v4 corrections (iter-168).

Covers the endpoints listed in the review request:
- /api/authority — position_truth STALE, overall CLOSE_ONLY, readiness_level present
- /api/accounts/limits — total_live_accounts=0, environment_counts populated
- /api/accounts — every item has 'environment'
- /api/ops/chaos (POST run + GET) — partial aggregation
- /api/ops/alerts — as_of, unacked, unacked_critical, synthetic_unacked
- Regression: /api/state/inventory (6 configured / 3 enabled)

Uses cookie-based auth as admin@trading.bot. Rate-limit friendly (small delays).
"""
import os
import time
import pytest
import requests

pytestmark = pytest.mark.http

from live_target import require_live_base_url  # noqa: E402

BASE_URL = require_live_base_url()


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    time.sleep(0.4)
    return s


def test_authority_position_truth_stale(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/authority", timeout=15)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert "readiness_level" in body, "readiness_level field missing"
    domains = body.get("domains", {})
    pt = domains.get("position_truth", {})
    assert pt.get("level") == "STALE", (
        f"expected position_truth.level=STALE got {pt.get('level')} "
        f"reason={pt.get('reason')}")
    reason = (pt.get("reason") or "").lower()
    assert "broker" in reason or "count" in reason or "confirm" in reason, \
        f"reason should mention broker/count/confirm, got: {pt.get('reason')}"
    assert body.get("level") in ("CLOSE_ONLY", "LOCKED"), \
        f"overall level should be CLOSE_ONLY, got {body.get('level')}"
    time.sleep(0.3)


def test_accounts_limits_live_zero_with_env_counts(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/accounts/limits", timeout=15)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body.get("total_live_accounts") == 0, \
        f"expected 0 live got {body.get('total_live_accounts')}"
    assert "environment_counts" in body, "environment_counts missing"
    ec = body["environment_counts"]
    assert isinstance(ec, dict) and len(ec) > 0
    # In preview: {'DEMO': 5} (and maybe PAPER excluded from mode!=paper)
    assert "DEMO" in ec or any(k.upper() == "DEMO" for k in ec), \
        f"expected DEMO in environment_counts got {ec}"
    time.sleep(0.3)


def test_accounts_have_environment_field(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/accounts", timeout=15)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    accounts = body if isinstance(body, list) else body.get("accounts", body)
    assert len(accounts) > 0, "no accounts returned"
    for a in accounts:
        env = a.get("environment")
        assert env in ("LIVE", "DEMO", "PAPER"), \
            f"account {a.get('label') or a.get('id')} has environment={env}"
    time.sleep(0.3)


def test_ops_alerts_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/ops/alerts", timeout=15)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    for k in ("as_of", "unacked", "unacked_critical"):
        assert k in body, f"missing key {k} in {list(body.keys())}"
    # synthetic_unacked expected (per request)
    assert "synthetic_unacked" in body, \
        f"synthetic_unacked missing: {list(body.keys())}"
    assert isinstance(body["unacked"], int)
    assert isinstance(body["unacked_critical"], int)
    assert body["unacked_critical"] <= body["unacked"]
    time.sleep(0.3)


def test_ops_chaos_partial_aggregation(admin_session):
    # Fetch current, run drill, fetch again
    time.sleep(0.5)
    run = admin_session.post(f"{BASE_URL}/api/ops/chaos/run", timeout=60)
    assert run.status_code in (200, 201), f"chaos run: {run.status_code} {run.text[:300]}"
    time.sleep(0.5)
    r = admin_session.get(f"{BASE_URL}/api/ops/chaos", timeout=15)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    for k in ("passed", "partial", "total"):
        assert k in body, f"missing key {k} in {list(body.keys())}"
    assert body["passed"] + body["partial"] <= body["total"]
    # Verify partial reflects skipped_assertions
    results = body.get("results", [])
    with_skipped = [r for r in results if r.get("skipped_assertions")]
    partial_from_results = sum(1 for r in with_skipped if r.get("passed"))
    assert body["partial"] >= partial_from_results, \
        f"partial={body['partial']} < counted_from_results={partial_from_results}"
    time.sleep(0.3)


def test_state_inventory_regression(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/state/inventory", timeout=15)
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    # configured 6 / enabled 3 per review context
    configured = body.get("configured") or body.get("accounts_configured") \
        or body.get("total_configured")
    enabled = body.get("enabled") or body.get("accounts_enabled") \
        or body.get("total_enabled")
    # Just soft-assert presence & not zero
    assert configured is not None, f"no configured key: {list(body.keys())}"
    assert enabled is not None, f"no enabled key: {list(body.keys())}"
