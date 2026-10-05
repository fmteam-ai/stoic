"""Live backend verification for Security & Health Agent SA1+SA2.

Covers:
- GET /api/admin/security/{status,findings,findings/{id},check-runs}
- 401 unauthenticated, 403 non-admin
- in-process tick: 28 check_runs, no 'failed', T1 open
- dedup: at most one open row per dedup_key
- event hook: bogus bridge token → 401 + security_events row kind='bridge_invalid_token'
- regression smoke: /api/ops/release-readiness, /api/admin/ops-console
- observe-mode invariants: security_blocks and security_actions empty
"""
from __future__ import annotations

import os
import sys
import time
import uuid

import pytest
import requests

# Make live_target importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_target import require_live_base_url, admin_credentials  # noqa: E402

BASE_URL = require_live_base_url().rstrip("/")
ADMIN_EMAIL, ADMIN_PASSWORD = admin_credentials(strict=True)


# -------- helpers --------

def _login(email: str, password: str) -> requests.Session:
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": email, "password": password},
        timeout=20,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    # mirror csrf cookie into header for POSTs
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers["X-CSRF-Token"] = csrf
    return s


@pytest.fixture(scope="module")
def admin_session() -> requests.Session:
    return _login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def mongo_db():
    """Direct DB handle to inspect security_* collections."""
    pymongo = pytest.importorskip("pymongo")
    url = os.environ.get("MONGO_URL") or "mongodb://localhost:27017"
    db_name = os.environ.get("STOIC_LIVE_DB_NAME") or "ai_trading_bot"
    client = pymongo.MongoClient(url, serverSelectionTimeoutMS=3000)
    db = client[db_name]
    db.command("ping")
    yield db
    client.close()


# -------- auth gates --------

def test_security_status_unauthenticated_401():
    r = requests.get(f"{BASE_URL}/api/admin/security/status", timeout=15)
    assert r.status_code == 401, (r.status_code, r.text[:200])


def test_security_findings_non_admin_403(mongo_db):
    """Create a throwaway non-admin user directly (register endpoint requires
    turnstile + email verification), login, verify 403."""
    from passlib.hash import bcrypt
    email = f"sa_probe_{uuid.uuid4().hex[:10]}@example.com"
    pw = "Pw-" + uuid.uuid4().hex + "#1"
    pw_hash = bcrypt.hash(pw)
    assert pw_hash.startswith("$2b$") or pw_hash.startswith("$2a$")
    from datetime import datetime, timezone
    doc = {
        "email": email,
        "password_hash": pw_hash,
        "name": "SA Probe",
        "role": "user",
        "status": "active",
        "email_verified": True,
        "two_factor_enabled": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "accepted_terms_version": "v1",
        "accepted_terms_at": datetime.now(timezone.utc).isoformat(),
        "_sa_probe": True,
    }
    ins = mongo_db.users.insert_one(doc)
    try:
        s = _login(email, pw)
        r = s.get(f"{BASE_URL}/api/admin/security/status", timeout=15)
        assert r.status_code == 403, (r.status_code, r.text[:200])
        r2 = s.get(f"{BASE_URL}/api/admin/security/findings", timeout=15)
        assert r2.status_code == 403, (r2.status_code, r2.text[:200])
    finally:
        mongo_db.users.delete_one({"_id": ins.inserted_id})


# -------- status --------

def test_security_status_shape(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/security/status", timeout=15)
    assert r.status_code == 200, r.text[:200]
    data = r.json()
    for k in ("light", "mode", "rules_enabled", "open_by_severity",
              "checks_total", "worker_lease"):
        assert k in data, f"missing key {k} in {list(data.keys())}"
    assert data["mode"] == "observe", data["mode"]
    assert data["checks_total"] == 28, data["checks_total"]
    assert data["rules_enabled"] == [], data["rules_enabled"]
    assert isinstance(data["open_by_severity"], dict)


# -------- findings --------

def test_security_findings_list_and_dedup(admin_session, mongo_db):
    # call findings twice — must not create duplicates
    r1 = admin_session.get(f"{BASE_URL}/api/admin/security/findings", timeout=15)
    assert r1.status_code == 200
    r2 = admin_session.get(f"{BASE_URL}/api/admin/security/findings", timeout=15)
    assert r2.status_code == 200
    data = r2.json()
    assert "findings" in data
    required = {"check_id", "dedup_key", "severity", "area", "title",
                "what_happened", "why_it_matters", "evidence", "solution",
                "verify", "status", "occurrences"}
    for f in data["findings"]:
        missing = required - set(f.keys())
        assert not missing, f"finding missing keys {missing}"

    # dedup — at most one open row per dedup_key in Mongo
    pipeline = [
        {"$match": {"status": {"$in": ["open", "contained", "acknowledged"]}}},
        {"$group": {"_id": "$dedup_key", "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
    ]
    dups = list(mongo_db.security_findings.aggregate(pipeline))
    assert dups == [], f"duplicate open findings by dedup_key: {dups}"


def test_security_finding_detail_404(admin_session):
    r = admin_session.get(
        f"{BASE_URL}/api/admin/security/findings/nope-invalid-id",
        timeout=15,
    )
    assert r.status_code == 404, (r.status_code, r.text[:200])


def test_security_finding_detail_ok(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/security/findings", timeout=15)
    assert r.status_code == 200
    findings = r.json().get("findings", [])
    if not findings:
        pytest.skip("no findings to fetch")
    fid = findings[0].get("id") or findings[0].get("_id") or findings[0].get("finding_id")
    if not fid:
        pytest.skip(f"finding has no id field: {list(findings[0].keys())}")
    r2 = admin_session.get(
        f"{BASE_URL}/api/admin/security/findings/{fid}",
        timeout=15,
    )
    assert r2.status_code == 200, (r2.status_code, r2.text[:200])


# -------- check runs --------

def test_security_check_runs(admin_session, mongo_db):
    r = admin_session.get(f"{BASE_URL}/api/admin/security/check-runs", timeout=15)
    assert r.status_code == 200, r.text[:200]
    data = r.json()
    runs = data.get("check_runs") or data.get("runs") or data.get("checks") or data
    if isinstance(runs, dict) and "check_runs" in runs:
        runs = runs["check_runs"]
    assert isinstance(runs, list), f"unexpected shape: {type(runs).__name__}"
    assert len(runs) == 28, f"expected 28 checks, got {len(runs)}"
    for c in runs:
        assert "interval_s" in c, f"check missing interval_s: {c}"
        # after at least one tick these should be present
        assert c.get("status") != "failed", f"check failed: {c.get('check_id')} {c}"

    # DB has latest:<check_id> docs for all 28 ids
    latest_docs = list(mongo_db.security_check_runs.find({"_id": {"$regex": "^latest:"}}))
    ids = {d["_id"].split(":", 1)[1] for d in latest_docs}
    assert len(ids) >= 28, f"expected ≥28 latest check_runs, got {len(ids)}: {sorted(ids)}"


# -------- backend log tick --------

def test_backend_log_tick_present():
    path = "/var/log/supervisor/backend.err.log"
    with open(path, "r", errors="ignore") as fh:
        content = fh.read()
    assert "security agent tick: 28 check(s) ran" in content, \
        "no '28 check(s) ran' tick line in backend.err.log"


# -------- T1 heartbeat finding expected open --------

def test_t1_ea_heartbeat_finding_open(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/security/findings", timeout=15)
    assert r.status_code == 200
    findings = r.json().get("findings", [])
    t1 = [f for f in findings if f.get("check_id") == "T1" and f.get("status") == "open"]
    assert t1, f"expected open T1 EA-heartbeat finding; got check_ids={[f.get('check_id') for f in findings]}"


# -------- event hook: bogus bridge token --------

def test_bridge_invalid_token_logged(mongo_db):
    before_iso = __import__("datetime").datetime.utcnow().isoformat()
    bogus = f"bogus-{uuid.uuid4().hex[:12]}"
    r = requests.post(
        f"{BASE_URL}/api/bridge/heartbeat",
        json={"bridge_token": bogus, "balance": 1, "equity": 1},
        timeout=15,
    )
    assert r.status_code == 401, (r.status_code, r.text[:200])
    # Give the hook a moment
    row = None
    for _ in range(15):
        row = mongo_db.security_events.find_one(
            {"kind": "bridge_invalid_token", "at": {"$gte": before_iso}},
            sort=[("at", -1)],
        )
        if row:
            break
        time.sleep(0.3)
    assert row, "no security_events row kind=bridge_invalid_token recorded after the probe"
    assert row.get("ip"), f"security_events row missing ip: {row}"


# -------- observe-mode invariants --------

def test_observe_mode_no_blocks_or_actions(mongo_db):
    # Observe only — no containment yet
    blocks = mongo_db.security_blocks.count_documents({})
    actions = mongo_db.security_actions.count_documents({})
    assert blocks == 0, f"security_blocks must be empty in observe mode, got {blocks}"
    assert actions == 0, f"security_actions must be empty in observe mode, got {actions}"


# -------- regression smoke --------

def test_release_readiness_smoke(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/ops/release-readiness", timeout=20)
    # 200 (ready) or 503 (not-ready in preview — workers unhealthy) both acceptable;
    # the request only asks that checks still include unique_ticket_index.
    assert r.status_code in (200, 503), (r.status_code, r.text[:200])
    data = r.json()
    checks = data.get("checks") or {}
    assert "unique_ticket_index" in checks, f"missing unique_ticket_index: {list(checks.keys())[:10]}"


def test_admin_ops_console_smoke(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/ops-console", timeout=20)
    assert r.status_code == 200, (r.status_code, r.text[:200])
