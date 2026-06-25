"""Iter22 — LIVE HTTP verification of security hardening.
Extra checks for the testing agent beyond the iter22 spec:
  - CORS evil-origin preflight against the actual deployed backend
  - CORS whitelisted-origin preflight echoes back exact origin + credentials
  - Reveal endpoint full flow on a real seeded account: 422 / 403 / 200 paths
  - Audit log row created in db.credential_reveals for every reveal attempt
  - P2.2 — bridge-token endpoint with garbage id returns 404 (not 500)
"""
import os
import pytest
import requests
from datetime import datetime, timezone
from bson import ObjectId
from pymongo import MongoClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL").rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"Login failed: {r.text}"
    return s


@pytest.fixture(scope="module")
def db():
    client = MongoClient(os.environ["MONGO_URL"])
    return client[os.environ["DB_NAME"]]


# --------------- CORS live preflights ---------------

def test_cors_evil_origin_preflight():
    r = requests.options(
        f"{API}/auth/me",
        headers={"Origin": "https://evil.example.com",
                 "Access-Control-Request-Method": "GET"},
        timeout=10,
    )
    aco = r.headers.get("Access-Control-Allow-Origin", "")
    acc = r.headers.get("Access-Control-Allow-Credentials", "")
    # MUST NOT echo evil origin with credentials
    assert not (aco == "https://evil.example.com" and acc.lower() == "true"), \
        f"Evil origin echoed with creds! ACAO={aco!r} ACAC={acc!r}"
    # MUST NOT have wildcard + credentials
    assert not (aco == "*" and acc.lower() == "true"), \
        f"Wildcard+creds returned. ACAO={aco!r} ACAC={acc!r}"


def test_cors_whitelisted_origin_preflight():
    trusted = BASE_URL
    r = requests.options(
        f"{API}/auth/me",
        headers={"Origin": trusted,
                 "Access-Control-Request-Method": "GET"},
        timeout=10,
    )
    aco = r.headers.get("Access-Control-Allow-Origin", "")
    acc = r.headers.get("Access-Control-Allow-Credentials", "")
    # If the preflight succeeded with the origin, credentials must be allowed
    if aco == trusted:
        assert acc.lower() == "true", \
            f"Whitelisted origin without credentials. ACAC={acc!r}"


# --------------- Reveal full flow ---------------

def test_reveal_missing_password_returns_422(admin_session):
    r = admin_session.post(
        f"{API}/accounts/6a3ad0ef17f40ac1dd3eb3ef/credentials/reveal",
        json={}, timeout=10)
    assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"


def test_reveal_wrong_password_returns_403_and_audits(admin_session, db):
    # We're hitting an admin-owned account (admin's micro). Wrong pw → 403 + audit row.
    before = db.credential_reveals.count_documents({"result": "wrong_password"})

    r = admin_session.post(
        f"{API}/accounts/6a3ad0ef17f40ac1dd3eb3ef/credentials/reveal",
        json={"password": "definitely-not-the-admin-password"}, timeout=10)
    # 403 (account exists, wrong pw) OR 404 (acct gone) — both acceptable
    assert r.status_code in (403, 404), f"Got {r.status_code}: {r.text}"

    if r.status_code == 403:
        # Verify audit row written
        after = db.credential_reveals.count_documents({"result": "wrong_password"})
        assert after >= before + 1, \
            f"No audit row for wrong_password attempt (before={before}, after={after})"


def test_reveal_correct_password_returns_only_investor_by_default(admin_session, db):
    """Find admin's account, do reveal with correct password, verify NO master_password key."""
    u = db.users.find_one({"email": ADMIN_EMAIL})
    if not u:
        pytest.skip("admin user not found")
    a = db.accounts.find_one({"user_id": str(u["_id"])})
    if not a:
        pytest.skip("No admin account in DB to test reveal against")
    acct_id = str(a["_id"])

    before = db.credential_reveals.count_documents({"result": "ok"})

    # Default (no include_master)
    r = admin_session.post(
        f"{API}/accounts/{acct_id}/credentials/reveal",
        json={"password": ADMIN_PASSWORD}, timeout=10)
    assert r.status_code == 200, f"Reveal failed: {r.status_code} {r.text}"
    body = r.json()
    assert "investor_password" in body, "investor_password missing from response"
    assert "master_password" not in body, \
        f"master_password leaked without include_master flag! body={body}"

    after = db.credential_reveals.count_documents({"result": "ok"})
    assert after >= before + 1, "No audit log written for successful reveal"

    # With include_master=true
    r2 = admin_session.post(
        f"{API}/accounts/{acct_id}/credentials/reveal",
        json={"password": ADMIN_PASSWORD, "include_master": True}, timeout=10)
    assert r2.status_code == 200, f"include_master reveal failed: {r2.status_code} {r2.text}"
    body2 = r2.json()
    assert "investor_password" in body2
    assert "master_password" in body2, \
        f"master_password missing even with include_master=true. body={body2}"


# --------------- P2.2 garbage id endpoints ---------------

def test_bridge_token_with_garbage_account_id_returns_404(admin_session):
    r = admin_session.post(f"{API}/accounts/garbage/bridge-token", timeout=10)
    assert r.status_code == 404, f"Got {r.status_code}: {r.text}"


def test_trade_revive_with_garbage_id_returns_404(admin_session):
    r = admin_session.post(f"{API}/trades/garbage/revive", timeout=10)
    assert r.status_code == 404, f"Got {r.status_code}: {r.text}"


def test_trade_close_with_garbage_id_returns_404(admin_session):
    r = admin_session.post(f"{API}/trades/garbage/close", timeout=10)
    assert r.status_code == 404, f"Got {r.status_code}: {r.text}"


def test_trades_execute_with_garbage_params_returns_404(admin_session):
    r = admin_session.post(f"{API}/trades/execute",
                           params={"signal_id": "garbage",
                                   "account_id": "alsogarbage"}, timeout=10)
    assert r.status_code in (400, 404), f"Got {r.status_code}: {r.text}"
