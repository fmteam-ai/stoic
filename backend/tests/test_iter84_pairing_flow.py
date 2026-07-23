"""iter-84 · Pairing-token flow tests.

Covers:
  · POST /api/setup/pairing-token requires auth + ownership
  · Issuing a new token invalidates any prior outstanding one
  · POST /api/setup/claim-pairing (UNauth'd) — happy path returns bridge_token
  · Bad / consumed / expired tokens return structured 400 with codes
  · After claim, /api/setup/pairing-status reflects paired state
  · Paper accounts rejected at issue time (no MT5 to install into)
  · GET /api/setup/installer.ps1 serves the PowerShell script
"""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import requests
from bson import ObjectId
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL"):
                BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
if not BASE_URL.startswith("http"):
    BASE_URL = "https://" + BASE_URL
TIMEOUT = 30


def _mongo():
    with open(_os.path.join(_BACKEND_DIR, ".env")) as f:
        cfg = {ln.split("=", 1)[0]: ln.split("=", 1)[1].strip().strip("\"'")
               for ln in f if "=" in ln}
    return MongoClient(cfg["MONGO_URL"])[cfg["DB_NAME"]]


def _register_verified() -> tuple[str, str, requests.Session]:
    suffix = uuid.uuid4().hex[:10]
    email, pw = f"iter84_{suffix}@example.com", "Gy6#Vb3kM9zRnD2s"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": pw,
                            "name": f"iter84-{suffix}", "terms_agreed": True},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    _mongo().users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"email_verified": True},
         "$unset": {"activation_token": "", "activation_expires_at": ""}},
    )
    s = requests.Session()
    s.post(f"{BASE_URL}/api/auth/login",
           json={"email": email, "password": pw}, timeout=TIMEOUT)
    return uid, email, s


def _add_live_account(sess: requests.Session, broker: str = "STARTRADER") -> str:
    acct = "iter84-" + uuid.uuid4().hex[:8]
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": f"iter84-{acct}", "broker": broker, "server": "T-Demo",
        "account_number": acct, "account_type": "demo",
        "base_currency": "USD", "mode": "live",
    }, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture
def cleanup():
    refs = {"user_ids": [], "account_ids": []}
    yield refs
    db = _mongo()
    if refs["user_ids"]:
        db.users.delete_many({"_id": {"$in": refs["user_ids"]}})
    if refs["account_ids"]:
        db.accounts.delete_many({"_id": {"$in": refs["account_ids"]}})
        db.pairing_tokens.delete_many({"account_id": {"$in": [str(a) for a in refs["account_ids"]]}})


# ─────────────────── Issue token ───────────────────
def test_issue_pairing_token_happy_path(cleanup):
    uid, _, sess = _register_verified()
    aid = _add_live_account(sess)
    cleanup["user_ids"].append(ObjectId(uid))
    cleanup["account_ids"].append(ObjectId(aid))

    r = sess.post(f"{BASE_URL}/api/setup/pairing-token",
                  json={"account_id": aid}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"]
    assert body["ttl_minutes"] == 15
    assert body["broker"] == "STARTRADER"
    # Token persisted with no consumed_at yet
    pairing = _mongo().pairing_tokens.find_one({"token": body["token"]})
    assert pairing
    assert pairing["account_id"] == aid
    assert pairing.get("consumed_at") is None


def test_pairing_token_requires_auth(cleanup):
    # Create an account via a real user, then try to issue token unauth'd
    uid, _, sess = _register_verified()
    aid = _add_live_account(sess)
    cleanup["user_ids"].append(ObjectId(uid))
    cleanup["account_ids"].append(ObjectId(aid))

    r = requests.post(f"{BASE_URL}/api/setup/pairing-token",
                      json={"account_id": aid}, timeout=TIMEOUT)
    assert r.status_code in (401, 403)


def test_pairing_token_rejects_other_users_account(cleanup):
    uid_a, _, sess_a = _register_verified()
    uid_b, _, sess_b = _register_verified()
    aid = _add_live_account(sess_a)
    cleanup["user_ids"] += [ObjectId(uid_a), ObjectId(uid_b)]
    cleanup["account_ids"].append(ObjectId(aid))

    r = sess_b.post(f"{BASE_URL}/api/setup/pairing-token",
                    json={"account_id": aid}, timeout=TIMEOUT)
    assert r.status_code == 403


def test_pairing_token_rejected_for_paper_account(cleanup):
    uid, _, sess = _register_verified()
    cleanup["user_ids"].append(ObjectId(uid))
    paper = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": "paper", "broker": "INTERNAL_PAPER", "server": "paper-virtual",
        "account_number": "PAPER1", "account_type": "demo",
        "base_currency": "USD", "mode": "paper", "initial_balance": 10000,
    }, timeout=TIMEOUT)
    assert paper.status_code == 200
    pid = paper.json()["id"]
    cleanup["account_ids"].append(ObjectId(pid))

    r = sess.post(f"{BASE_URL}/api/setup/pairing-token",
                  json={"account_id": pid}, timeout=TIMEOUT)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "paper_account"


def test_reissuing_invalidates_prior_token(cleanup):
    uid, _, sess = _register_verified()
    aid = _add_live_account(sess)
    cleanup["user_ids"].append(ObjectId(uid))
    cleanup["account_ids"].append(ObjectId(aid))

    t1 = sess.post(f"{BASE_URL}/api/setup/pairing-token",
                   json={"account_id": aid}, timeout=TIMEOUT).json()["token"]
    t2 = sess.post(f"{BASE_URL}/api/setup/pairing-token",
                   json={"account_id": aid}, timeout=TIMEOUT).json()["token"]
    assert t1 != t2
    # Prior token is gone — claim attempt returns invalid_token.
    r = requests.post(f"{BASE_URL}/api/setup/claim-pairing",
                      json={"token": t1}, timeout=TIMEOUT)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_token"


# ─────────────────── Claim ───────────────────
def test_claim_happy_path_returns_bridge_token_and_consumes(cleanup):
    uid, _, sess = _register_verified()
    aid = _add_live_account(sess)
    cleanup["user_ids"].append(ObjectId(uid))
    cleanup["account_ids"].append(ObjectId(aid))

    token = sess.post(f"{BASE_URL}/api/setup/pairing-token",
                      json={"account_id": aid}, timeout=TIMEOUT).json()["token"]
    # Claim is UNauth'd — installer doesn't have cookies.
    r = requests.post(f"{BASE_URL}/api/setup/claim-pairing",
                      json={"token": token, "hostname": "test-vps-01",
                            "installer_version": "1.0"},
                      timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bridge_token"]
    assert body["broker"] == "STARTRADER"
    assert body["ea_script_url"].endswith("/api/ea-script")
    assert body["heartbeat_url"].endswith("/api/bridge/heartbeat")

    # Token is now consumed
    pairing = _mongo().pairing_tokens.find_one({"token": token})
    assert pairing.get("consumed_at")
    assert pairing.get("consumed_by_hostname") == "test-vps-01"

    # Account marked as paired
    acct = _mongo().accounts.find_one({"_id": ObjectId(aid)})
    assert acct.get("installer_paired_at")
    assert acct.get("installer_paired_hostname") == "test-vps-01"


def test_claim_replay_blocked(cleanup):
    uid, _, sess = _register_verified()
    aid = _add_live_account(sess)
    cleanup["user_ids"].append(ObjectId(uid))
    cleanup["account_ids"].append(ObjectId(aid))

    token = sess.post(f"{BASE_URL}/api/setup/pairing-token",
                      json={"account_id": aid}, timeout=TIMEOUT).json()["token"]
    first = requests.post(f"{BASE_URL}/api/setup/claim-pairing",
                          json={"token": token}, timeout=TIMEOUT)
    assert first.status_code == 200

    second = requests.post(f"{BASE_URL}/api/setup/claim-pairing",
                           json={"token": token}, timeout=TIMEOUT)
    assert second.status_code == 400
    assert second.json()["detail"]["code"] == "already_used"


def test_claim_expired_token_400(cleanup):
    uid, _, sess = _register_verified()
    aid = _add_live_account(sess)
    cleanup["user_ids"].append(ObjectId(uid))
    cleanup["account_ids"].append(ObjectId(aid))

    token = sess.post(f"{BASE_URL}/api/setup/pairing-token",
                      json={"account_id": aid}, timeout=TIMEOUT).json()["token"]
    # Backdate expiry directly.
    _mongo().pairing_tokens.update_one(
        {"token": token},
        {"$set": {"expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}},
    )
    r = requests.post(f"{BASE_URL}/api/setup/claim-pairing",
                      json={"token": token}, timeout=TIMEOUT)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "expired_token"


def test_claim_unknown_token_400():
    r = requests.post(f"{BASE_URL}/api/setup/claim-pairing",
                      json={"token": "not-a-real-token-xxxxxx"}, timeout=TIMEOUT)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_token"


# ─────────────────── Status polling ───────────────────
def test_pairing_status_reflects_consumption(cleanup):
    uid, _, sess = _register_verified()
    aid = _add_live_account(sess)
    cleanup["user_ids"].append(ObjectId(uid))
    cleanup["account_ids"].append(ObjectId(aid))

    # Before issuing — no token, no paired_at
    pre = sess.get(f"{BASE_URL}/api/setup/pairing-status/{aid}", timeout=TIMEOUT).json()
    assert pre["installer_paired_at"] is None
    assert pre["token_outstanding"] is False

    token = sess.post(f"{BASE_URL}/api/setup/pairing-token",
                      json={"account_id": aid}, timeout=TIMEOUT).json()["token"]
    issued = sess.get(f"{BASE_URL}/api/setup/pairing-status/{aid}", timeout=TIMEOUT).json()
    assert issued["token_outstanding"] is True
    assert issued["installer_paired_at"] is None

    requests.post(f"{BASE_URL}/api/setup/claim-pairing",
                  json={"token": token, "hostname": "stoic-vps"}, timeout=TIMEOUT)
    after = sess.get(f"{BASE_URL}/api/setup/pairing-status/{aid}", timeout=TIMEOUT).json()
    assert after["installer_paired_at"] is not None
    assert after["installer_paired_hostname"] == "stoic-vps"
    assert after["token_outstanding"] is False
    assert after["token_consumed_at"] is not None


# ─────────────────── Installer endpoint ───────────────────
def test_installer_ps1_endpoint_serves_script():
    r = requests.get(f"{BASE_URL}/api/setup/installer.ps1", timeout=TIMEOUT)
    assert r.status_code == 200
    body = r.text
    assert "Install-Stoic" in body
    assert "claim-pairing" in body
    assert "MetaQuotes" in body
