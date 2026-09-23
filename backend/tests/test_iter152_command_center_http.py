from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-152 — HTTP auth gating for Command Center endpoints.

Verifies:
  * anonymous → 401
  * non-admin logged-in user → 403
  * admin → 200 with expected schema on /status
  * admin → 200 with Content-Disposition attachment header on /evidence-export
    and a hash-chained report that verifies via command_center.verify_report
"""
import os
import uuid
import pytest
import requests

from live_target import require_live_base_url
BASE = require_live_base_url()
API = BASE + "/api"

pass  # ADMIN_EMAIL comes from live_target
ADMIN_PW = ADMIN_PASSWORD

STRONG_PW = "Kd5#Zt9mW2xVpR7c"


def _login(email, pw):
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": email, "password": pw}, timeout=15)
    return s, r


@pytest.fixture(scope="module")
def admin_session():
    s, r = _login(ADMIN_EMAIL, ADMIN_PW)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
    return s


@pytest.fixture(scope="module")
def user_session():
    email = f"cc_user_{uuid.uuid4().hex[:8]}@example.com"
    s = requests.Session()
    r = s.post(f"{API}/auth/register", json={
        "email": email, "password": STRONG_PW,
        "name": "CC Test", "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), f"register: {r.status_code} {r.text[:200]}"
    # flip email_verified via a helper if needed; but /auth/register may allow immediate login.
    # If login fails due to email_verified=false, we need a bypass. Try login first.
    lr = s.post(f"{API}/auth/login", json={"email": email, "password": STRONG_PW}, timeout=15)
    if lr.status_code != 200:
        # Directly flip via mongo
        import pymongo
        mongo = pymongo.MongoClient(os.environ.get("MONGO_URL", "mongodb://localhost:27017"))
        db_name = os.environ.get("DB_NAME", "stoic_trading")
        mongo[db_name].users.update_one({"email": email}, {"$set": {"email_verified": True}})
        lr = s.post(f"{API}/auth/login", json={"email": email, "password": STRONG_PW}, timeout=15)
    assert lr.status_code == 200, f"user login: {lr.status_code} {lr.text[:200]}"
    return s


def test_status_anonymous_denied():
    r = requests.get(f"{API}/command-center/status", timeout=15)
    assert r.status_code in (401, 403), f"expected 401/403, got {r.status_code}"


def test_evidence_export_anonymous_denied():
    r = requests.get(f"{API}/command-center/evidence-export", timeout=15)
    assert r.status_code in (401, 403)


def test_status_non_admin_forbidden(user_session):
    r = user_session.get(f"{API}/command-center/status", timeout=15)
    assert r.status_code == 403, f"expected 403 for non-admin, got {r.status_code}: {r.text[:200]}"


def test_evidence_export_non_admin_forbidden(user_session):
    r = user_session.get(f"{API}/command-center/evidence-export", timeout=15)
    assert r.status_code == 403


def test_status_admin_ok(admin_session):
    r = admin_session.get(f"{API}/command-center/status", timeout=30)
    assert r.status_code == 200, f"admin /status: {r.status_code} {r.text[:200]}"
    data = r.json()
    assert data["overall"] in ("GREEN", "YELLOW", "RED")
    for sec in ("soak", "certifications", "guard", "workers"):
        assert sec in data["sections"]
        assert "status" in data["sections"][sec]
        assert "detail" in data["sections"][sec]
    assert "recent_alerts" in data
    assert "recent_alert_emails" in data
    assert "email_alerts_configured" in data
    assert "provenance" in data
    assert data["provenance"].get("guard_policy_version")


def test_evidence_export_admin_download(admin_session):
    r = admin_session.get(f"{API}/command-center/evidence-export", timeout=30)
    assert r.status_code == 200
    cd = r.headers.get("Content-Disposition", "")
    assert "attachment" in cd.lower() and ".json" in cd.lower(), cd
    report = r.json()
    assert report.get("report") == "stoic-production-evidence"
    names = [s["section"] for s in report["sections"]]
    assert names == ["provenance", "certifications", "soak_status",
                     "soak_evidence_chain", "guard_health",
                     "release_canary"]
    for s in report["sections"]:
        assert "prev_hash" in s and "hash" in s
    assert "report_hash" in report

    # Verify via backend's own verify_report
    import sys
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".."))
    from command_center import verify_report
    assert verify_report(report) is True

    # Tamper → verify fails
    import copy
    t = copy.deepcopy(report)
    t["sections"][0]["data"]["git_commit"] = "tampered"
    assert verify_report(t) is False


pytestmark = pytest.mark.http
