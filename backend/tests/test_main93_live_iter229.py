"""Live HTTP suite for main93 review (iteration 229).

Covers:
 - P1-02 access-token revocation on logout + sessions/revoke-all
 - P1-03 forced password change gating (403 password_change_required on /api/accounts, 200 on /me)
 - P1-01 hashed bridge tokens (rotate step-up, no plaintext in list/doc, heartbeat auth, revoke)
 - A7d execution_brake release admin-only
 - GET /api/health release_identity + ea_version

NOTE: conftest.py auto-injects X-Step-Up-Bypass and X-CSRF-Token on every requests
call. To exercise the REAL step-up gate, set session.headers["X-Step-Up-Bypass"] = ""
(the empty string suppresses the autofill).
"""
import os
import uuid
import pytest
import requests
import bcrypt
from pymongo import MongoClient

pytestmark = pytest.mark.http

BASE = os.environ.get("REACT_APP_BACKEND_URL", "https://stoic-trading-bot.preview.emergentagent.com").rstrip("/")
STEP_UP = os.environ.get("STEP_UP_BYPASS_TOKEN") or ""          # from backend/.env (preview) / CI secret — never inline
MONGO = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB = os.environ.get("DB_NAME", "ai_trading_bot")

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL") or "admin@stoicaibot.com"
_P = os.environ.get("TEST_ADMIN_PASSWORD") or ""                 # audit round 7 P1 — credentials NEVER embedded in source
if not (_P and STEP_UP):
    pytest.skip("TEST_ADMIN_PASSWORD / STEP_UP_BYPASS_TOKEN not in the environment", allow_module_level=True)


def _mkadmin_session():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": _P}, timeout=15)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def admin_session():
    return _mkadmin_session()


@pytest.fixture(scope="module")
def mongo():
    return MongoClient(MONGO)[DB]


# ---------- Health / release identity ----------

def test_health_release_identity_and_ea_version(admin_session):
    r = requests.get(f"{BASE}/api/health", timeout=10)
    assert r.status_code == 200
    j = r.json()
    assert j.get("ea_version") == "1.59", j.get("ea_version")
    # audit r30 — release identity is admin-only; the public probe no longer carries it
    assert "release_identity" not in j
    assert requests.get(f"{BASE}/api/health/release", timeout=10).status_code == 401
    r2 = admin_session.get(f"{BASE}/api/health/release", timeout=10)
    assert r2.status_code == 200, r2.text
    ri = r2.json().get("release_identity")
    assert isinstance(ri, dict), j
    assert "image_digest" in ri
    assert ri.get("ea_shipped_version") == "1.59"
    assert isinstance(ri.get("ea_accepted_sha256s"), list)


# ---------- P1-02 ----------

def test_p1_02_logout_revokes_access_token():
    s = _mkadmin_session()
    r1 = s.get(f"{BASE}/api/auth/me", timeout=10)
    assert r1.status_code == 200
    # Capture cookies BEFORE logout; logout clears them client-side so we need
    # to re-send the SAME access cookie manually to prove the server rejects it.
    saved_cookies = {c.name: c.value for c in s.cookies}
    r2 = s.post(f"{BASE}/api/auth/logout", timeout=10)
    assert r2.status_code in (200, 204), r2.text
    # Re-send saved cookies — the backend must now refuse the token with session_revoked
    r3 = requests.get(f"{BASE}/api/auth/me", cookies=saved_cookies, timeout=10)
    assert r3.status_code == 401, (r3.status_code, r3.text[:200])
    assert "session_revoked" in r3.text.lower(), r3.text


def test_p1_02_revoke_all_kills_current_access_token():
    s = _mkadmin_session()
    saved_cookies = {c.name: c.value for c in s.cookies}
    r2 = s.post(f"{BASE}/api/auth/sessions/revoke-all", timeout=10)
    assert r2.status_code in (200, 204), r2.text
    # The current access token (captured before) must now be refused
    r3 = requests.get(f"{BASE}/api/auth/me", cookies=saved_cookies, timeout=10)
    assert r3.status_code == 401
    assert "session_revoked" in r3.text.lower() or "revoked" in r3.text.lower(), r3.text


# ---------- P1-03 forced password change (seed user directly in Mongo) ----------

def test_p1_03_must_change_password_gates_but_me_works(mongo):
    email = f"test_pwchg_{uuid.uuid4().hex[:8]}@example.com"
    pw = "TempPassw0rd!AB" + "cDe123"
    pw_hash = bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    mongo.users.insert_one({
        "id": str(uuid.uuid4()),
        "email": email, "password_hash": pw_hash, "name": "T", "role": "user",
        "must_change_password": True, "email_verified": True, "two_factor_enabled": False,
    })
    try:
        s = requests.Session()
        rl = s.post(f"{BASE}/api/auth/login", json={"email": email, "password": pw}, timeout=15)
        assert rl.status_code == 200, rl.text
        rme = s.get(f"{BASE}/api/auth/me", timeout=10)
        assert rme.status_code == 200
        ra = s.get(f"{BASE}/api/accounts", timeout=10)
        assert ra.status_code == 403, (ra.status_code, ra.text[:300])
        assert "password_change_required" in ra.text.lower(), ra.text[:300]
        new_pw = "NewTempPassw0rd!XY" + "zA12"
        rc = s.post(f"{BASE}/api/auth/change-password",
                    json={"current_password": pw, "new_password": new_pw}, timeout=15)
        assert rc.status_code in (200, 204), (rc.status_code, rc.text[:300])
        # Re-login after password change (session typically revoked)
        s2 = requests.Session()
        rl2 = s2.post(f"{BASE}/api/auth/login", json={"email": email, "password": new_pw}, timeout=15)
        assert rl2.status_code == 200, rl2.text
        ra2 = s2.get(f"{BASE}/api/accounts", timeout=10)
        assert ra2.status_code == 200, (ra2.status_code, ra2.text[:300])
    finally:
        mongo.users.delete_one({"email": email})


# ---------- P1-01 hashed bridge tokens ----------

@pytest.fixture(scope="module")
def created_account(admin_session, mongo):
    # randomize account_number to avoid duplicate_account 409
    anum = str(90000000 + (uuid.uuid4().int % 9000000))
    payload = {
        "label": f"TEST_acct_{uuid.uuid4().hex[:6]}",
        "account_number": anum,
        "broker": "MT5",
        "server": "TestServer",
        "terms_accepted": True,
    }
    r = admin_session.post(f"{BASE}/api/accounts", json=payload, timeout=15)
    assert r.status_code in (200, 201), r.text
    body = r.json()
    acc_id = body.get("id")
    yield {"id": acc_id, "account_number": anum}
    try:
        admin_session.delete(f"{BASE}/api/accounts/{acc_id}", timeout=10)
    except Exception:
        pass
    mongo.accounts.delete_many({"label": {"$regex": "^TEST_"}})


_shared = {}


def test_p1_01_rotate_token_requires_step_up(admin_session, created_account):
    # Disable the auto-bypass for this call ONLY
    admin_session.headers["X-Step-Up-Bypass"] = ""
    try:
        r = admin_session.post(f"{BASE}/api/accounts/{created_account['id']}/rotate-token", timeout=15)
    finally:
        del admin_session.headers["X-Step-Up-Bypass"]
    assert r.status_code == 403, (r.status_code, r.text[:300])
    # Could be either 'step_up_required' or 'mfa_enrollment_required' when 2FA not enrolled
    assert "step_up" in r.text.lower() or "mfa_enrollment" in r.text.lower(), r.text[:300]


def test_p1_01_rotate_returns_plaintext_once_and_doc_has_hash(admin_session, created_account, mongo):
    r = admin_session.post(f"{BASE}/api/accounts/{created_account['id']}/rotate-token", timeout=15)
    assert r.status_code == 200, r.text
    j = r.json()
    plaintext = j.get("bridge_token")
    assert plaintext and isinstance(plaintext, str), j
    _shared["bt"] = plaintext
    _shared["acc_num"] = created_account["account_number"]

    # Mongo accounts doc uses ObjectId _id — fetch by label
    from bson import ObjectId
    try:
        doc = mongo.accounts.find_one({"_id": ObjectId(created_account["id"])})
    except Exception:
        doc = mongo.accounts.find_one({"id": created_account["id"]})
    assert doc is not None, "account not found in mongo"
    assert "bridge_token_hash" in doc, list(doc.keys())
    assert "bridge_token_last4" in doc
    assert "bridge_token" not in doc, "plaintext bridge_token must NOT be stored"

    # heartbeat with plaintext authenticates
    rhb = requests.post(f"{BASE}/api/bridge/heartbeat",
                        json={"bridge_token": plaintext, "ea_version": "1.59",
                              "account_login": created_account["account_number"],
                              "balance": 0.0, "equity": 0.0, "open_positions": 0}, timeout=15)
    assert rhb.status_code == 200, (rhb.status_code, rhb.text[:400])

    # list must not expose token/hash
    rl = admin_session.get(f"{BASE}/api/accounts", timeout=10)
    assert rl.status_code == 200
    accs = rl.json() if isinstance(rl.json(), list) else rl.json().get("accounts", [])
    this_acc = next((a for a in accs if a.get("id") == created_account["id"]), None)
    assert this_acc is not None
    assert "bridge_token" not in this_acc
    assert "bridge_token_hash" not in this_acc
    assert this_acc.get("has_bridge_token") is True
    assert "bridge_token_masked" in this_acc


def test_p1_01_revoke_requires_step_up_and_disables_token(admin_session, created_account):
    plaintext = _shared.get("bt")
    assert plaintext, "prior rotate test did not run"
    # revoke without step-up
    admin_session.headers["X-Step-Up-Bypass"] = ""
    try:
        r1 = admin_session.post(f"{BASE}/api/accounts/{created_account['id']}/bridge-token/revoke", timeout=10)
    finally:
        del admin_session.headers["X-Step-Up-Bypass"]
    assert r1.status_code == 403, (r1.status_code, r1.text[:200])
    # revoke with step-up (auto-bypassed)
    r2 = admin_session.post(f"{BASE}/api/accounts/{created_account['id']}/bridge-token/revoke", timeout=10)
    assert r2.status_code in (200, 204), r2.text
    # heartbeat now 401
    rhb = requests.post(f"{BASE}/api/bridge/heartbeat",
                        json={"bridge_token": plaintext, "ea_version": "1.59",
                              "account_login": _shared.get("acc_num"),
                              "balance": 0.0, "equity": 0.0, "open_positions": 0}, timeout=10)
    assert rhb.status_code == 401, (rhb.status_code, rhb.text[:200])


# ---------- A7d execution brake ----------

def test_a7d_execution_brake_release_admin_works(admin_session, created_account, mongo):
    from bson import ObjectId
    oid = ObjectId(created_account["id"])
    mongo.accounts.update_one({"_id": oid},
                                {"$set": {"execution_brake": {"active": True, "reason": "test"}}})
    r = admin_session.post(f"{BASE}/api/accounts/{created_account['id']}/execution-brake/release", timeout=10)
    assert r.status_code in (200, 204), (r.status_code, r.text[:300])
    doc = mongo.accounts.find_one({"_id": oid})
    brk = (doc or {}).get("execution_brake") or {}
    assert not brk.get("active"), brk
