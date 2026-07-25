"""iter-98 HTTP surface — safety hardening end-to-end verification.

Covers: grandfathered admin autonomous_live, fresh-user observe default,
step-up gate on mode promotion, governance approve-without-MFA blocking,
release-readiness loop_progress, and audit-log evidence trail.
"""
import os
import sys
import uuid
import time
import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import base_url, register_and_login, mongo_db  # noqa: E402

API = f"{base_url()}/api"
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@trading.bot")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")


@pytest.fixture(scope="module")
def admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    assert r.status_code == 200, r.text
    return s


# --------------------------------------------- grandfather (admin) mode
def test_admin_config_grandfathered_supervised_live(admin):
    """iter-103: no config silently keeps autonomous authority — legacy
    active configs were re-migrated to supervised_live."""
    r = admin.get(f"{API}/bot/config", timeout=15)
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg.get("operational_mode") in ("supervised_live",
                                           "autonomous_live"), (
        f"admin config expected a live mode, got {cfg.get('operational_mode')}")
    if cfg.get("operational_mode") == "autonomous_live":
        # only allowed when explicitly promoted through the gate
        assert cfg.get("mode_explicitly_promoted") is True


# ----------------------------------------------- fresh user fail-safe
def test_fresh_user_default_observe():
    email = f"iter98-obs-{uuid.uuid4().hex[:8]}@example.com"
    s = register_and_login(email)
    r = s.get(f"{API}/bot/config", timeout=15)
    assert r.status_code == 200, r.text
    assert r.json().get("operational_mode") == "observe"


# ----------------------------------------------- step-up gate on promotion
def test_fresh_user_promotion_requires_stepup():
    email = f"iter98-promo-{uuid.uuid4().hex[:8]}@example.com"
    s = register_and_login(email)
    # Force real gate — disable the auto-inject bypass header
    s.headers["X-Step-Up-Bypass"] = ""
    r = s.put(f"{API}/bot/config",
              json={"operational_mode": "supervised_live"}, timeout=15)
    assert r.status_code == 403, f"expected 403 got {r.status_code}: {r.text}"
    body = r.json()
    code = body.get("detail", {}).get("code") if isinstance(body.get("detail"), dict) else body.get("code")
    assert code in ("step_up_required", "mfa_enrollment_required"), body


def test_fresh_user_promotion_with_bypass_header_succeeds():
    email = f"iter98-promo2-{uuid.uuid4().hex[:8]}@example.com"
    s = register_and_login(email)
    bypass = os.environ.get("STEP_UP_BYPASS_TOKEN")
    assert bypass, "STEP_UP_BYPASS_TOKEN not set in env"
    # conftest auto-injects; verify success path
    r = s.put(f"{API}/bot/config",
              json={"operational_mode": "supervised_live"},
              headers={"X-Step-Up-Bypass": bypass}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json().get("operational_mode") == "supervised_live"

    # Demotion back to observe — no MFA needed
    s.headers["X-Step-Up-Bypass"] = ""
    r2 = s.put(f"{API}/bot/config",
               json={"operational_mode": "observe"}, timeout=15)
    assert r2.status_code == 200, r2.text
    assert r2.json().get("operational_mode") == "observe"


# ------------------------------------------ governance approve MFA gate
def test_governance_approve_without_mfa_blocked_reject_ok(admin):
    # Propose a small change from admin — safe target that admin owns.
    # We deliberately reject at the end so live config is UNCHANGED.
    r_cfg = admin.get(f"{API}/bot/config", timeout=15)
    current_risk = r_cfg.json().get("risk_pct", 0.5)
    new_risk = round(min(2.0, current_risk + 0.05), 3)

    r = admin.post(f"{API}/governance/propose",
                   json={"field": "risk_pct",
                         "new_value": new_risk,
                         "source": "iter98_test"},
                   timeout=15)
    assert r.status_code == 200, r.text
    change = r.json()
    cid = change.get("_id") or change.get("id") or change.get("change_id")
    assert cid, f"no change id in {change}"

    try:
        # APPROVE without step-up — hit real gate
        admin.headers["X-Step-Up-Bypass"] = ""
        ra = admin.post(f"{API}/governance/changes/{cid}/approve",
                        json={"reason": "iter98 test approve without mfa"},
                        timeout=15)
        assert ra.status_code == 403, f"expected 403 got {ra.status_code}: {ra.text}"
        body = ra.json()
        code = body.get("detail", {}).get("code") if isinstance(body.get("detail"), dict) else body.get("code")
        assert code in ("mfa_enrollment_required", "step_up_required"), body
    finally:
        # Restore bypass then REJECT to clean up
        bypass = os.environ.get("STEP_UP_BYPASS_TOKEN")
        if bypass:
            admin.headers["X-Step-Up-Bypass"] = bypass
        rr = admin.post(f"{API}/governance/changes/{cid}/reject",
                        json={"reason": "iter98 test cleanup — always reject"},
                        timeout=15)
        assert rr.status_code == 200, f"reject cleanup failed: {rr.text}"


# ------------------------------------------ release-readiness loop progress
def test_release_readiness_loop_progress(admin):
    r = admin.get(f"{API}/ops/release-readiness", timeout=15)
    # Endpoint returns 503 when overall ready=false (e.g., workers stale),
    # but the loop_progress SUB-check should still be present and ok.
    assert r.status_code in (200, 503), r.text
    data = r.json()
    checks = data.get("checks", {})
    lp = checks.get("loop_progress")
    assert lp is not None, f"loop_progress missing: {list(checks.keys())}"
    assert lp.get("ok") is True, f"loop_progress not ok: {lp}"
    detail = lp.get("detail") or {}
    assert "bot_runner.loop" in detail, detail
    assert "trade_manager.run_loop" in detail, detail
    for k in ("bot_runner.loop", "trade_manager.run_loop"):
        row = detail[k]
        assert row.get("last_iteration_completed_at")
        assert isinstance(row.get("last_duration_ms"), int)
        assert isinstance(row.get("processed_count"), int)


# -------------------------------------------- audit-log trail (mongo direct)
def test_audit_log_has_migration_and_governance_entries():
    db = mongo_db()
    mig = db.audit_log.find_one({"action": "operational_mode_migration"})
    assert mig is not None, "no migration audit entry"
    details = mig.get("detail") or mig.get("details") or {}
    grand = details.get("grandfathered_active_to_autonomous_live")
    assert isinstance(grand, int) and grand >= 0

    reject_evt = db.audit_log.find_one({"action": "governance_reject"},
                                       sort=[("_id", -1)])
    assert reject_evt is not None, "no governance_reject audit entry"


# --------------------------------------------- regression endpoints still 200
@pytest.mark.parametrize("path", [
    "/performance/verified",
    "/learning/failure-summary",
    "/risk/regime",
    "/learning/speeds",
])
def test_regression_endpoints_ok(admin, path):
    r = admin.get(f"{API}{path}", timeout=20)
    assert r.status_code == 200, f"{path} → {r.status_code}: {r.text[:200]}"
