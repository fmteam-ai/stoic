"""Live backend verification for Security & Health Agent SA3.

Covers:
- GET /api/admin/security/status (SA1 shape — regression)
- GET /api/admin/security/actions (default + ?kind=containment filter) → shape+rules R1..R8
- GET /api/admin/security/reports/daily?build=true (JSON + ?format=html)
- GET /api/admin/security/reports/weekly?build=true (JSON) incl. trends/fp_rate/tuning_suggestions
- GET /api/admin/security/reports/{bogus} → 404
- GET /api/admin/security/reports/daily  (stored, if available)  → 200 or 404 (acceptable)
- POST /api/admin/security/mode without X-Step-Up-Bypass → 403
- POST /api/admin/security/mode {mode:'observe', rules_enabled:[]} with bypass → 200
- POST /api/admin/security/mode {mode:'bogus'} → 400
- POST /api/admin/security/mode {mode:'observe', rules_enabled:['R99']} → 400
- POST /api/admin/security/test-alert → 200, telegram_configured:false
- Finding alert_count>=1 after ~65s + security_actions kind='alert' row exists
- POST /findings/{id}/status acknowledged → 200
- POST /findings/{id}/status resolved → 200
- Replay resolve → 404 "finding not open"
- status 'deleted' → 400
- Non-admin → 403
- Unauthenticated → 401
- GET /api/admin/audit/verify ok=true after writes
- Dedup invariant for critical/high findings
- Observe-mode invariants (security_blocks empty, no 'done' actions)

ALWAYS restores observe mode at module teardown.
"""
from __future__ import annotations

import os
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_target import require_live_base_url, admin_credentials  # noqa: E402

BASE_URL = require_live_base_url().rstrip("/")
ADMIN_EMAIL, ADMIN_PASSWORD = admin_credentials(strict=True)

STEP_UP_BYPASS = os.environ.get("STEP_UP_BYPASS_TOKEN") or ""


def _read_env_token(key: str) -> str:
    """Fallback: read /app/backend/.env for the token."""
    try:
        with open("/app/backend/.env", "r") as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


if not STEP_UP_BYPASS:
    STEP_UP_BYPASS = _read_env_token("STEP_UP_BYPASS_TOKEN")


# -------- helpers --------
def _login(email: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": email, "password": password}, timeout=20)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers["X-CSRF-Token"] = csrf
    return s


@pytest.fixture(scope="module")
def admin_session() -> requests.Session:
    assert STEP_UP_BYPASS, "STEP_UP_BYPASS_TOKEN not available"
    return _login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def step_up_headers() -> dict:
    return {"X-Step-Up-Bypass": STEP_UP_BYPASS}


@pytest.fixture(scope="module")
def mongo_db():
    pymongo = pytest.importorskip("pymongo")
    url = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
    db_name = os.environ.get("STOIC_LIVE_DB_NAME") or "ai_trading_bot"
    client = pymongo.MongoClient(url, serverSelectionTimeoutMS=3000)
    db = client[db_name]
    db.command("ping")
    yield db
    client.close()


@pytest.fixture(scope="module", autouse=True)
def restore_observe_mode(admin_session, step_up_headers):
    """Hard guarantee: regardless of what tests set, leave mode=observe."""
    yield
    try:
        admin_session.post(
            f"{BASE_URL}/api/admin/security/mode",
            json={"mode": "observe", "rules_enabled": []},
            headers=step_up_headers, timeout=15,
        )
    except Exception:
        pass


# -------- status (regression) --------
def test_status_sa1_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/security/status", timeout=15)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d["mode"] == "observe"
    assert d["checks_total"] == 28
    assert "light" in d and d["light"] in ("amber", "green", "red")
    assert "open_by_severity" in d


# -------- actions --------
def test_actions_shape_and_rules(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/security/actions", timeout=15)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert "actions" in d and isinstance(d["actions"], list)
    assert "rules" in d and isinstance(d["rules"], dict)
    for rid in ("R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"):
        assert rid in d["rules"], f"missing rule {rid}: keys={list(d['rules'].keys())}"
        rule = d["rules"][rid]
        for k in ("title", "checks", "action"):
            assert k in rule, f"{rid} missing {k}"


def test_actions_kind_filter(admin_session):
    r = admin_session.get(
        f"{BASE_URL}/api/admin/security/actions?kind=containment", timeout=15)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert "actions" in d
    for a in d["actions"]:
        assert a.get("kind") == "containment", f"wrong kind: {a}"


# -------- reports --------
def test_report_daily_build_json(admin_session):
    r = admin_session.get(
        f"{BASE_URL}/api/admin/security/reports/daily?build=true", timeout=25)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d.get("kind") == "daily", d.get("kind")
    for k in ("new_findings", "still_open", "checks_failed",
              "checks_silent", "counts"):
        assert k in d, f"missing key {k} in daily report: {list(d.keys())}"


def test_report_daily_build_html(admin_session):
    r = admin_session.get(
        f"{BASE_URL}/api/admin/security/reports/daily?build=true&format=html",
        timeout=25)
    assert r.status_code == 200, r.text[:200]
    ct = r.headers.get("content-type", "")
    assert "text/html" in ct, f"content-type: {ct}"
    assert "<" in r.text[:500]


def test_report_weekly_build_json(admin_session):
    r = admin_session.get(
        f"{BASE_URL}/api/admin/security/reports/weekly?build=true", timeout=30)
    assert r.status_code == 200, r.text[:200]
    d = r.json()
    assert d.get("kind") == "weekly"
    for k in ("trends_by_area", "false_positive_rate", "tuning_suggestions"):
        assert k in d, f"missing weekly key {k}: {list(d.keys())}"


def test_report_bogus_404(admin_session):
    r = admin_session.get(
        f"{BASE_URL}/api/admin/security/reports/bogus", timeout=15)
    assert r.status_code == 404, (r.status_code, r.text[:200])


def test_report_daily_stored(admin_session):
    """Stored daily may or may not exist in preview; both 200 and 404 acceptable."""
    r = admin_session.get(
        f"{BASE_URL}/api/admin/security/reports/daily", timeout=20)
    assert r.status_code in (200, 404), (r.status_code, r.text[:200])


# -------- mode (step-up) --------
def test_mode_without_bypass_403(admin_session):
    # Conftest's requests-session patch auto-injects X-Step-Up-Bypass via
    # setdefault. Pass the header explicitly as "" to force the real gate.
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/mode",
        json={"mode": "observe", "rules_enabled": []},
        headers={"X-Step-Up-Bypass": ""}, timeout=15)
    assert r.status_code == 403, (r.status_code, r.text[:200])
    body = r.json()
    detail = body.get("detail") or {}
    code = (detail.get("code") if isinstance(detail, dict) else None) or ""
    assert code in ("step_up_required", "mfa_enrollment_required"), f"code={code} body={body}"


def test_mode_bogus_400(admin_session, step_up_headers):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/mode",
        json={"mode": "bogus"}, headers=step_up_headers, timeout=15)
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_mode_bad_rule_400(admin_session, step_up_headers):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/mode",
        json={"mode": "observe", "rules_enabled": ["R99"]},
        headers=step_up_headers, timeout=15)
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_mode_observe_ok(admin_session, step_up_headers):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/mode",
        json={"mode": "observe", "rules_enabled": []},
        headers=step_up_headers, timeout=15)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    d = r.json()
    assert d.get("mode") == "observe"


# -------- test-alert end-to-end --------
@pytest.fixture(scope="module")
def test_alert_finding_id(admin_session, step_up_headers):
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/test-alert",
        headers=step_up_headers, timeout=15)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    d = r.json()
    assert "finding_id" in d and d["finding_id"]
    assert d.get("telegram_configured") is False
    return d["finding_id"]


def test_test_alert_delivers_within_65s(admin_session, mongo_db, test_alert_finding_id):
    """Within ~65s the next tick runs alerts.sweep → alert_count>=1,
    last_alert_channels present with telegram:false, email:0."""
    fid = test_alert_finding_id
    from bson import ObjectId
    deadline = time.time() + 75
    row = None
    while time.time() < deadline:
        row = mongo_db.security_findings.find_one({"_id": ObjectId(fid)})
        if row and int(row.get("alert_count") or 0) >= 1:
            break
        time.sleep(2)
    assert row and int(row.get("alert_count") or 0) >= 1, \
        f"alert_count still 0 after wait: alert_count={row.get('alert_count') if row else None}"
    chans = row.get("last_alert_channels") or {}
    assert chans.get("telegram") is False, f"telegram should be false unconfigured: {chans}"
    assert int(chans.get("email", 0)) == 0, f"email should be 0: {chans}"

    # security_actions row kind='alert' for this finding
    act = mongo_db.security_actions.find_one(
        {"kind": "alert", "finding_id": fid})
    if not act:
        # some impls key by ObjectId
        act = mongo_db.security_actions.find_one(
            {"kind": "alert", "finding_id": ObjectId(fid)})
    assert act, f"no security_actions row kind='alert' for finding {fid}"


def test_actions_alert_filter_contains_alert(admin_session, test_alert_finding_id):
    r = admin_session.get(
        f"{BASE_URL}/api/admin/security/actions?kind=alert", timeout=15)
    assert r.status_code == 200, r.text[:200]
    acts = r.json().get("actions", [])
    assert acts, "no alert actions returned by API"
    assert any(a.get("kind") == "alert" for a in acts)


def test_finding_ack_then_resolve(admin_session, step_up_headers, test_alert_finding_id):
    fid = test_alert_finding_id
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/findings/{fid}/status",
        json={"status": "acknowledged"},
        headers=step_up_headers, timeout=15)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert r.json().get("status") == "acknowledged"

    r2 = admin_session.post(
        f"{BASE_URL}/api/admin/security/findings/{fid}/status",
        json={"status": "resolved"},
        headers=step_up_headers, timeout=15)
    assert r2.status_code == 200, (r2.status_code, r2.text[:200])
    body = r2.json()
    assert body.get("fixed") in ("yes", True, "y") or body.get("status") == "resolved", f"body={body}"

    # replay resolve → 404 "finding not open"
    r3 = admin_session.post(
        f"{BASE_URL}/api/admin/security/findings/{fid}/status",
        json={"status": "resolved"},
        headers=step_up_headers, timeout=15)
    assert r3.status_code == 404, (r3.status_code, r3.text[:200])
    assert "not open" in r3.text.lower()


def test_finding_status_deleted_400(admin_session, step_up_headers):
    # Use any valid-ish ObjectId
    bogus = "507f1f77bcf86cd799439011"
    r = admin_session.post(
        f"{BASE_URL}/api/admin/security/findings/{bogus}/status",
        json={"status": "deleted"},
        headers=step_up_headers, timeout=15)
    assert r.status_code == 400, (r.status_code, r.text[:200])


# -------- auth gates --------
def test_mode_unauthenticated_401():
    r = requests.post(f"{BASE_URL}/api/admin/security/mode",
                      json={"mode": "observe"}, timeout=15)
    assert r.status_code == 401, (r.status_code, r.text[:200])


def test_test_alert_non_admin_403(mongo_db):
    from passlib.hash import bcrypt
    email = f"sa3_probe_{uuid.uuid4().hex[:10]}@example.com"
    pw = "Pw-" + uuid.uuid4().hex + "#1"
    doc = {
        "email": email, "password_hash": bcrypt.hash(pw),
        "name": "SA3 Probe", "role": "user", "status": "active",
        "email_verified": True, "two_factor_enabled": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "accepted_terms_version": "v1",
        "accepted_terms_at": datetime.now(timezone.utc).isoformat(),
        "_sa_probe": True,
    }
    ins = mongo_db.users.insert_one(doc)
    try:
        s = _login(email, pw)
        r = s.post(f"{BASE_URL}/api/admin/security/test-alert", timeout=15)
        assert r.status_code == 403, (r.status_code, r.text[:200])
    finally:
        mongo_db.users.delete_one({"_id": ins.inserted_id})


# -------- audit chain --------
def test_audit_verify_ok(admin_session, mongo_db):
    r = admin_session.get(f"{BASE_URL}/api/admin/audit/verify", timeout=20)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    d = r.json()
    assert d.get("ok") is True, f"audit chain not ok: {d}"

    # entries for security_agent should exist
    n = mongo_db.admin_audit_log.count_documents({"target_kind": "security_agent"})
    assert n >= 1, f"no admin_audit_log entries target_kind=security_agent"


# -------- dedup invariant --------
def test_dedup_alert_count_invariant(mongo_db):
    """Every open critical/high finding: alert_count <= 1 UNLESS critical AND
    >30 min since last_alert_at."""
    now = datetime.now(timezone.utc)
    bad = []
    for f in mongo_db.security_findings.find({
        "status": {"$in": ["open", "acknowledged", "contained"]},
        "severity": {"$in": ["critical", "high"]},
    }):
        ac = int(f.get("alert_count") or 0)
        if ac <= 1:
            continue
        if f.get("severity") != "critical":
            bad.append({"id": str(f.get("_id")), "sev": f.get("severity"), "ac": ac})
            continue
        laa = f.get("last_alert_at")
        if isinstance(laa, str):
            try:
                laa_dt = datetime.fromisoformat(laa.replace("Z", "+00:00"))
            except Exception:
                laa_dt = None
        elif isinstance(laa, datetime):
            laa_dt = laa if laa.tzinfo else laa.replace(tzinfo=timezone.utc)
        else:
            laa_dt = None
        if not laa_dt or (now - laa_dt) < timedelta(minutes=30):
            bad.append({"id": str(f.get("_id")), "ac": ac, "laa": str(laa)})
    assert not bad, f"dedup violation(s): {bad}"


# -------- observe-mode invariants --------
def test_observe_invariants_no_done_actions(mongo_db):
    assert mongo_db.security_blocks.count_documents({}) == 0, \
        "security_blocks must be empty in observe mode"
    assert mongo_db.security_actions.count_documents({"status": "done"}) == 0, \
        "no security_actions row should have status='done' in observe mode"


# -------- regression --------
def test_admin_ops_console_smoke(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/ops-console", timeout=20)
    assert r.status_code == 200, (r.status_code, r.text[:200])


def test_backend_log_no_stage_failed():
    with open("/var/log/supervisor/backend.err.log", "r", errors="ignore") as fh:
        content = fh.read()
    assert "security agent tick" in content, "no tick lines in backend log"
    assert "stage failed" not in content.lower(), \
        "found 'stage failed' warning in backend log"
