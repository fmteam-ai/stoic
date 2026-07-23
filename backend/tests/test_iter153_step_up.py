"""iter-153 · Step-up MFA — fresh TOTP required before live-sensitive ops:
live activation, risk raises, panic release, API-key creation. Plus the
append-only security audit trail (db.audit_log, GET /api/auth/audit)."""
import os as _os
import sys as _sys
from datetime import datetime, timezone

import pyotp
import requests

_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
_sys.path.insert(0, _BACKEND_DIR)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(_os.path.join(_BACKEND_DIR, ".env"))

BASE = open(_os.path.join(_REPO_DIR, "frontend", ".env")).read().split(
    "REACT_APP_BACKEND_URL=")[1].splitlines()[0].strip()

EMAIL = "stepup-test@trading.bot"
PASSWORD = "Stepup-Pass-123"


def _db():
    from pymongo import MongoClient
    client = MongoClient(_os.environ["MONGO_URL"])
    return client[_os.environ["DB_NAME"]]


def _ensure_user():
    """Idempotent seed: verified user with a live account, no 2FA."""
    import bcrypt
    db = _db()
    pw_hash = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode()
    db.users.update_one(
        {"email": EMAIL},
        {"$set": {"password_hash": pw_hash, "email_verified": True,
                  "status": "active", "role": "user",
                  "two_factor_enabled": False},
         "$unset": {"totp_secret": "", "totp_secret_pending": "",
                    "recovery_codes": ""},
         "$setOnInsert": {"name": "StepUp Test", "created_at": "2026-06-01T00:00:00+00:00"}},
        upsert=True)
    uid = str(db.users.find_one({"email": EMAIL})["_id"])
    db.accounts.update_one(
        {"user_id": uid, "label": "stepup-live"},
        {"$set": {"mode": "live", "status": "connected", "dormant": False,
                  "last_heartbeat": datetime.now(timezone.utc).isoformat()},
         "$setOnInsert": {"bridge_token": "stepup-test-" + uid[-8:]}},
        upsert=True)
    db.bot_configs.update_many({"user_id": uid}, {"$set": {"active": False}})
    return uid


def _login():
    _ensure_user()
    s = requests.Session()
    # Force the REAL step-up gate (defeat the suite-wide test bypass).
    s.headers["X-Step-Up-Bypass"] = ""
    r = s.post(f"{BASE}/api/auth/login",
               json={"email": EMAIL, "password": PASSWORD}, timeout=15)
    assert r.status_code == 200, r.text
    return s


def _enroll_2fa(s):
    r = s.post(f"{BASE}/api/auth/2fa/enroll", json={}, timeout=15)
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    code = pyotp.TOTP(secret).now()
    r = s.post(f"{BASE}/api/auth/2fa/verify-enroll", json={"code": code},
               timeout=15)
    assert r.status_code == 200, r.text
    return secret


def _step_up(s, secret, action):
    r = s.post(f"{BASE}/api/auth/step-up",
               json={"code": pyotp.TOTP(secret).now(), "action": action},
               timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == action and body["expires_in"] == 300
    return body["step_up_token"]


# --------------------------------------------- unenrolled users are blocked
def test_unenrolled_user_blocked_from_live_start():
    s = _login()
    r = s.post(f"{BASE}/api/bot/start", timeout=15)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "mfa_enrollment_required"


def test_unenrolled_user_blocked_from_api_key_create():
    s = _login()
    r = s.post(f"{BASE}/api/api-keys",
               json={"name": "t", "scopes": ["read:trades"]}, timeout=15)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "mfa_enrollment_required"


# --------------------------------------------------- full happy-path flow
def test_step_up_full_flow():
    s = _login()
    secret = _enroll_2fa(s)

    # 1. enrolled but no token → step_up_required
    r = s.post(f"{BASE}/api/bot/start", timeout=15)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "step_up_required"

    # 2. wrong code rejected
    r = s.post(f"{BASE}/api/auth/step-up",
               json={"code": "000000", "action": "live_activation"}, timeout=15)
    assert r.status_code == 401

    # 3. unknown action rejected
    r = s.post(f"{BASE}/api/auth/step-up",
               json={"code": pyotp.TOTP(secret).now(), "action": "nope"},
               timeout=15)
    assert r.status_code == 400

    # 4. valid code → token → live start succeeds
    token = _step_up(s, secret, "live_activation")
    r = s.post(f"{BASE}/api/bot/start",
               headers={"X-Step-Up-Token": token}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True

    # 5. token is single-use
    s.post(f"{BASE}/api/bot/stop", timeout=15)
    r = s.post(f"{BASE}/api/bot/start",
               headers={"X-Step-Up-Token": token}, timeout=15)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "step_up_invalid"

    # 6. token action must match (api_key token can't start bot)
    tok2 = _step_up(s, secret, "api_key_create")
    r = s.post(f"{BASE}/api/bot/start",
               headers={"X-Step-Up-Token": tok2}, timeout=15)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "step_up_invalid"

    # 7. api-key creation with matching token
    r = s.post(f"{BASE}/api/api-keys",
               json={"name": "stepup-key", "scopes": ["read:trades"]},
               headers={"X-Step-Up-Token": tok2}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["api_key"].strip()

    # 8. audit trail recorded the events
    r = s.get(f"{BASE}/api/auth/audit", timeout=15)
    assert r.status_code == 200
    actions = [e["action"] for e in r.json()]
    assert "step_up_verified" in actions
    assert "live_activation" in actions
    assert "api_key_created" in actions


def test_risk_raise_requires_step_up():
    s = _login()
    secret = _enroll_2fa(s)
    # Baseline: ensure current risk_level low via non-risk field update path
    db = _db()
    uid = str(db.users.find_one({"email": EMAIL})["_id"])
    db.bot_configs.update_many({"user_id": uid}, {"$set": {"risk_level": "low"}})

    # Raising risk without a token → blocked
    r = s.put(f"{BASE}/api/bot/config", json={"risk_level": "high"}, timeout=15)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "step_up_required"

    # Lowering / equal risk needs no token
    r = s.put(f"{BASE}/api/bot/config", json={"risk_level": "low"}, timeout=15)
    assert r.status_code == 200, r.text

    # Non-risk fields need no token
    r = s.put(f"{BASE}/api/bot/config", json={"let_winners_run": True}, timeout=15)
    assert r.status_code == 200, r.text

    # With token the raise goes through
    token = _step_up(s, secret, "risk_raise")
    r = s.put(f"{BASE}/api/bot/config", json={"risk_level": "high"},
              headers={"X-Step-Up-Token": token}, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["risk_level"] == "high"
    # restore
    s.put(f"{BASE}/api/bot/config", json={"risk_level": "low"}, timeout=15)


def test_panic_release_action_when_tripped():
    s = _login()
    secret = _enroll_2fa(s)
    # Panic first (no step-up needed to STOP trading — only to resume)
    r = s.post(f"{BASE}/api/panic", timeout=15)
    assert r.status_code == 200, r.text

    r = s.post(f"{BASE}/api/bot/start", timeout=15)
    assert r.status_code == 403
    det = r.json()["detail"]
    assert det["code"] == "step_up_required" and det["action"] == "panic_release"

    token = _step_up(s, secret, "panic_release")
    r = s.post(f"{BASE}/api/bot/start",
               headers={"X-Step-Up-Token": token}, timeout=15)
    assert r.status_code == 200, r.text
    s.post(f"{BASE}/api/bot/stop", timeout=15)


# ------------------------------------- safety-status consolidated panel
def test_safety_status_new_fields():
    s = _login()
    r = s.get(f"{BASE}/api/bot/safety-status", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deployment_mode"] in ("LIVE", "MIXED", "PAPER", "IDLE")
    assert "capital_at_risk" in body
    assert "reconciliation_delay_sec" in body
    assert "panic_active" in body
