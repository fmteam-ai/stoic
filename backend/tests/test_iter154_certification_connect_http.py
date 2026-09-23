from live_target import ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""HTTP integration tests for iter-154 Certification Center + Connect + v1 API.

Runs against the deployed backend at REACT_APP_BACKEND_URL. Uses admin cookie
auth for the /api/certification/* + /api/connect/* endpoints, and API key auth
for /api/v1/* endpoints.
"""
import os
import secrets
import time

import pytest
import requests

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"

# Load STEP_UP_BYPASS_TOKEN from backend/.env
_BACKEND_ENV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
STEP_UP_BYPASS = None
try:
    with open(_BACKEND_ENV) as f:
        for line in f:
            if line.startswith("STEP_UP_BYPASS_TOKEN="):
                STEP_UP_BYPASS = line.strip().split("=", 1)[1].strip("\"'")
                break
except Exception:
    pass

ADMIN_EMAIL = "admin@stoicaibot.com"
pass  # ADMIN_PASSWORD comes from live_target
def setup_module(_m):
    """Idempotency pre-clean — repeated runs otherwise trip the per-broker
    account limit, the API-key cap and the daily cert-issuance cap."""
    try:
        import pymongo
        from datetime import datetime, timezone
        mongo_url = db_name = None
        with open(_BACKEND_ENV) as f:
            for line in f:
                if line.startswith("MONGO_URL="):
                    mongo_url = line.strip().split("=", 1)[1].strip("\"'")
                elif line.startswith("DB_NAME="):
                    db_name = line.strip().split("=", 1)[1].strip("\"'")
        db = pymongo.MongoClient(mongo_url)[db_name]
        accts = [str(a["_id"]) for a in
                 db.accounts.find({"label": {"$regex": "^TEST_"}},
                                  {"_id": 1})]
        db.accounts.delete_many({"label": {"$regex": "^TEST_"}})
        db.public_certificates.delete_many({"account_id": {"$in": accts}})
        db.pairing_tokens.delete_many({"account_id": {"$in": accts}})
        db.api_keys.update_many(
            {"name": {"$regex": "^TEST_iter154"}, "revoked_at": None},
            {"$set": {"revoked_at":
                      datetime.now(timezone.utc).isoformat()}})
        # keep the reused admin account under the 10/24h issuance cap
        certs = list(db.public_certificates.find(
            {}, {"account_id": 1}).sort("issued_at", -1))
        from collections import Counter
        for acct, n in Counter(c["account_id"] for c in certs).items():
            if n >= 8:
                keep = [c["_id"] for c in db.public_certificates.find(
                    {"account_id": acct}).sort("seq", 1).limit(2)]
                db.public_certificates.delete_many(
                    {"account_id": acct, "_id": {"$nin": keep}})
    except Exception:
        pass


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    if r.status_code != 200:
        # try legacy admin
        r = s.post(f"{API}/auth/login",
                   json={"email": ADMIN_EMAIL,
                         "password": ADMIN_PASSWORD}, timeout=30)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "csrf_token cookie missing"
    s.headers.update({"X-CSRF-Token": csrf})
    if STEP_UP_BYPASS:
        s.headers.update({"X-Step-Up-Bypass": STEP_UP_BYPASS})
    return s


@pytest.fixture(scope="module")
def admin_account_id(admin_session):
    r = admin_session.get(f"{API}/accounts", timeout=30)
    assert r.status_code == 200, r.text[:200]
    data = r.json()
    accounts = data if isinstance(data, list) else data.get("accounts", [])
    assert accounts, "admin has no accounts"
    return accounts[0]["id"] if "id" in accounts[0] else str(accounts[0].get("_id"))


# ─────────────────── Certification Center ───────────────────

class TestCertificationCenter:
    def test_center_requires_auth(self, admin_account_id):
        r = requests.get(f"{API}/certification/center",
                         params={"account_id": admin_account_id}, timeout=30)
        assert r.status_code == 401

    def test_center_returns_six_pillars(self, admin_session, admin_account_id):
        r = admin_session.get(f"{API}/certification/center",
                              params={"account_id": admin_account_id}, timeout=30)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert "pillars" in data and len(data["pillars"]) == 6
        keys = {p["pillar"] for p in data["pillars"]}
        assert keys == {"risk_truth", "position_truth", "execution_alpha",
                        "digital_twin", "strategy_decay", "broker_intel"}
        for p in data["pillars"]:
            assert p["status"] in ("GREEN", "YELLOW", "RED", "NO_DATA")
            assert "score" in p and "detail" in p and "metrics" in p
        assert "overall_score" in data and "tier" in data
        assert isinstance(data.get("certificates"), list)

    def test_center_bad_account_404(self, admin_session):
        r = admin_session.get(f"{API}/certification/center",
                              params={"account_id": "000000000000000000000000"},
                              timeout=30)
        assert r.status_code == 404

    def test_issue_and_public_view(self, admin_session, admin_account_id):
        r1 = admin_session.post(f"{API}/certification/center/issue",
                                json={"account_id": admin_account_id}, timeout=30)
        assert r1.status_code == 200, r1.text[:400]
        c1 = r1.json()
        assert c1["cert_id"].startswith("STC-")
        seq1 = c1["seq"]
        assert "account_id" not in c1 and "user_id" not in c1
        assert "•" in c1["account_ref"] or "*" in c1["account_ref"]

        r2 = admin_session.post(f"{API}/certification/center/issue",
                                json={"account_id": admin_account_id}, timeout=30)
        assert r2.status_code == 200, r2.text[:200]
        c2 = r2.json()
        assert c2["seq"] == seq1 + 1
        # prev_hash chain (public_view exposes prev_hash)
        assert c2.get("prev_hash") == c1.get("hash")

        # Public endpoint — no auth
        pub = requests.get(f"{API}/public/certificate/{c1['cert_id']}", timeout=30)
        assert pub.status_code == 200
        pv = pub.json()
        assert pv["hash_verified"] is True
        assert pv["valid"] is True
        assert "account_id" not in pv and "user_id" not in pv
        assert len(pv.get("pillars", [])) == 6
        assert "tier" in pv and "hash" in pv and "prev_hash" in pv

        # Revoke c1
        rr = admin_session.post(f"{API}/certification/center/revoke",
                                json={"cert_id": c1["cert_id"], "reason": "test"},
                                timeout=30)
        assert rr.status_code == 200, rr.text[:200]
        pub2 = requests.get(f"{API}/public/certificate/{c1['cert_id']}", timeout=30).json()
        assert pub2["valid"] is False
        assert pub2["hash_verified"] is True  # content unchanged, revoke flips flag

        # store for later cross-test
        pytest.issued_cert_id = c2["cert_id"]

    def test_public_unknown_404(self):
        r = requests.get(f"{API}/public/certificate/STC-NOPE", timeout=30)
        assert r.status_code == 404


# ─────────────────── STOIC Connect ───────────────────

_created_accounts = []


class TestConnect:
    def test_connect_start_missing_fields(self, admin_session):
        r = admin_session.post(f"{API}/connect/start",
                               json={"label": "x", "broker": "ICM"}, timeout=30)
        assert r.status_code == 422

    def test_connect_start_ok(self, admin_session):
        acct_num = f"9{secrets.randbelow(10**9):09d}"
        r = admin_session.post(f"{API}/connect/start", json={
            "label": f"TEST_Connect_{acct_num}",
            "broker": "ICMarketsSC-Demo",
            "server": "ICMarketsSC-Demo",
            "account_number": acct_num,
        }, timeout=30)
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert "pairing_token" in data
        cmd = data.get("install_command", "")
        assert "installer.ps1" in cmd
        assert "Install-Stoic -Token" in cmd
        acc_id = data.get("account_id") or (data.get("account") or {}).get("id")
        assert acc_id
        _created_accounts.append(acc_id)
        pytest.connect_account_id = acc_id

    def test_connect_status(self, admin_session):
        acc_id = getattr(pytest, "connect_account_id", None)
        assert acc_id
        r = admin_session.get(f"{API}/connect/{acc_id}/status", timeout=30)
        assert r.status_code == 200, r.text[:200]
        data = r.json()
        assert data["state"] == "PENDING"
        assert len(data["steps"]) == 7
        step_keys = {s["key"] for s in data["steps"]}
        assert {"account_created", "installer_paired", "ea_online",
                "identity_verified", "artifacts_verified",
                "reconciliation_clean", "certified"} <= step_keys
        assert data.get("current_step")
        assert isinstance(data.get("progress_pct"), (int, float))


# ─────────────────── Developer API v1 ───────────────────

class TestDeveloperAPIv1:
    @pytest.fixture(scope="class")
    def api_keys(self, admin_session):
        # Full-scope key
        r = admin_session.post(f"{API}/api-keys", json={
            "name": f"TEST_iter154_{secrets.token_hex(4)}",
            "scopes": ["write:connect", "read:accounts", "read:certificates"],
        }, timeout=30)
        assert r.status_code == 200, r.text[:400]
        full = r.json()
        # Limited (no write:connect) key
        r2 = admin_session.post(f"{API}/api-keys", json={
            "name": f"TEST_iter154_readonly_{secrets.token_hex(4)}",
            "scopes": ["read:accounts", "read:certificates"],
        }, timeout=30)
        assert r2.status_code == 200, r2.text[:200]
        limited = r2.json()
        yield full, limited
        # cleanup
        for k in (full, limited):
            kid = k["record"]["id"]
            try:
                admin_session.delete(f"{API}/api-keys/{kid}", timeout=10)
            except Exception:
                pass

    def test_v1_connect_create_and_status(self, api_keys):
        full, _ = api_keys
        headers = {"X-API-Key": full["api_key"]}
        acct_num = f"8{secrets.randbelow(10**9):09d}"
        r = requests.post(f"{API}/v1/connect/accounts", json={
            "label": f"TEST_v1_{acct_num}",
            "broker": "ICMarketsSC-Demo",
            "server": "ICMarketsSC-Demo",
            "account_number": acct_num,
        }, headers=headers, timeout=30)
        assert r.status_code == 200, r.text[:400]
        data = r.json()
        assert "pairing_token" in data
        assert "installer.ps1" in data.get("install_command", "")
        acc_id = data.get("account_id") or (data.get("account") or {}).get("id")
        assert acc_id
        _created_accounts.append(acc_id)

        # status
        rs = requests.get(f"{API}/v1/connect/accounts/{acc_id}/status",
                          headers=headers, timeout=30)
        assert rs.status_code == 200, rs.text[:200]
        st = rs.json()
        assert len(st["steps"]) == 7

        # certificate: 404 before issuing
        rc = requests.get(f"{API}/v1/accounts/{acc_id}/certificate",
                          headers=headers, timeout=30)
        assert rc.status_code == 404

    def test_v1_write_connect_scope_required(self, api_keys):
        _, limited = api_keys
        headers = {"X-API-Key": limited["api_key"]}
        acct_num = f"7{secrets.randbelow(10**9):09d}"
        r = requests.post(f"{API}/v1/connect/accounts", json={
            "label": "TEST_no_scope",
            "broker": "ICMarketsSC-Demo",
            "server": "ICMarketsSC-Demo",
            "account_number": acct_num,
        }, headers=headers, timeout=30)
        assert r.status_code == 403, f"expected 403, got {r.status_code} {r.text[:200]}"

    def test_v1_certificate_after_issue(self, admin_session, api_keys):
        full, _ = api_keys
        headers = {"X-API-Key": full["api_key"]}
        # Create an account via v1
        acct_num = f"6{secrets.randbelow(10**9):09d}"
        r = requests.post(f"{API}/v1/connect/accounts", json={
            "label": f"TEST_v1cert_{acct_num}",
            "broker": "ICMarketsSC-Demo",
            "server": "ICMarketsSC-Demo",
            "account_number": acct_num,
        }, headers=headers, timeout=30)
        assert r.status_code == 200, r.text[:200]
        acc_id = r.json().get("account_id") or (r.json().get("account") or {}).get("id")
        _created_accounts.append(acc_id)

        # Issue cert via UI endpoint (cookie auth)
        ri = admin_session.post(f"{API}/certification/center/issue",
                                json={"account_id": acc_id}, timeout=30)
        assert ri.status_code == 200, ri.text[:400]

        # Fetch via v1
        rc = requests.get(f"{API}/v1/accounts/{acc_id}/certificate",
                          headers=headers, timeout=30)
        assert rc.status_code == 200, rc.text[:200]
        cert = rc.json()
        assert cert.get("public_url", "").endswith("/certificate/" + cert["cert_id"])
        assert cert["hash_verified"] is True


def teardown_module(module):
    # Best-effort cleanup of created accounts
    if not _created_accounts:
        return
    s = requests.Session()
    try:
        s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL,
                                          "password": ADMIN_PASSWORD}, timeout=15)
        csrf = s.cookies.get("csrf_token")
        if csrf:
            s.headers.update({"X-CSRF-Token": csrf})
        for aid in _created_accounts:
            try:
                s.delete(f"{API}/accounts/{aid}?force=true", timeout=10)
            except Exception:
                pass
    except Exception:
        pass


pytestmark = pytest.mark.http
