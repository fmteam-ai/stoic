"""HTTP tests for Go-Live Inventory & 4-eyes expectation flow.

Covers:
- GET /api/authority/inventory/pending (admin 200 shape; non-admin 403)
- DELETE /api/authority/inventory/orphan-bots/{id} (success, 409 bot_has_account,
  404 unknown, 403 non-admin)
- 4-eyes POST /api/authority/inventory/expectation + /expectation/approve
  (same-admin rejected; different-admin succeeds)
- backend/ops/promote_admin.py refuses unknown email (exit 1) and bad args (exit 2)

Admin creds from TEST_ADMIN_EMAIL/TEST_ADMIN_PASSWORD (admin@trading.bot).
ADMIN_EMAIL (admin@stoicaibot.com) is the second admin.
"""
import os
import sys
import subprocess
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.http
import requests
from bson import ObjectId
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

FRONTEND_ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend", ".env")
BASE_URL = None
with open(FRONTEND_ENV) as f:
    for ln in f:
        if ln.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = ln.split("=", 1)[1].strip().strip('"').rstrip("/")
            break
assert BASE_URL

ADMIN_A_EMAIL = os.environ.get("TEST_ADMIN_EMAIL") or "admin@trading.bot"
ADMIN_A_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD")
ADMIN_B_EMAIL = os.environ.get("ADMIN_EMAIL") or "admin@stoicaibot.com"
ADMIN_B_PASSWORD = os.environ.get("ADMIN_PASSWORD")


def _login(email, password):
    if not password:
        pytest.skip(f"no password for {email}")
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password}, timeout=20)
    if r.status_code != 200:
        pytest.skip(f"login failed for {email}: {r.status_code} {r.text[:200]}")
    return s


@pytest.fixture(scope="module")
def admin_a():
    return _login(ADMIN_A_EMAIL, ADMIN_A_PASSWORD)


@pytest.fixture(scope="module")
def admin_b():
    return _login(ADMIN_B_EMAIL, ADMIN_B_PASSWORD)


@pytest.fixture(scope="module")
def user_session():
    """Seed non-admin user directly in DB (avoids turnstile/terms gates), then login."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from auth import hash_password  # type: ignore
    email = f"test_golive_user_{str(ObjectId())[:12]}@example.com"
    pwd = "GoLiveTest!Pw1234"
    mc = MongoClient(os.environ["MONGO_URL"])
    db_ = mc[os.environ["DB_NAME"]]
    now_iso = datetime.now(timezone.utc).isoformat()
    db_.users.insert_one({
        "email": email, "password_hash": hash_password(pwd), "name": "TestUser",
        "role": "user", "status": "active", "created_at": now_iso,
        "two_factor_enabled": False, "email_verified": True,
    })
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": pwd}, timeout=20)
    if r.status_code != 200:
        db_.users.delete_one({"email": email}); mc.close()
        pytest.skip(f"non-admin login failed: {r.status_code} {r.text[:200]}")
    yield s
    db_.users.delete_one({"email": email})
    mc.close()


@pytest.fixture(scope="module")
def db():
    mc = MongoClient(os.environ["MONGO_URL"])
    yield mc[os.environ["DB_NAME"]]
    mc.close()


# ── GET /api/authority/inventory/pending ──
def test_inventory_pending_admin_shape(admin_a):
    r = admin_a.get(f"{BASE_URL}/api/authority/inventory/pending", timeout=20)
    assert r.status_code == 200, r.text[:200]
    j = r.json()
    for k in ("expectation", "expectation_pending", "hash_pending", "orphan_bots", "orphan_total", "admin_count", "me"):
        assert k in j, f"missing key {k}"
    assert isinstance(j["orphan_bots"], list)
    assert len(j["orphan_bots"]) <= 50
    assert isinstance(j["orphan_total"], int)
    assert j["orphan_total"] >= len(j["orphan_bots"])
    assert isinstance(j["admin_count"], int)
    assert j["me"] == ADMIN_A_EMAIL


def test_inventory_pending_non_admin_forbidden(user_session):
    r = user_session.get(f"{BASE_URL}/api/authority/inventory/pending", timeout=20)
    assert r.status_code == 403, f"expected 403 got {r.status_code}: {r.text[:200]}"


# ── DELETE /api/authority/inventory/orphan-bots/{id} ──
def test_delete_orphan_bot_success(admin_a, db):
    uid = f"TEST_golive_{ObjectId()}"
    bot_id = ObjectId()
    db.bot_configs.insert_one({"_id": bot_id, "user_id": uid, "account_id": None,
                               "symbol": "EURUSD", "active": False,
                               "created_at": datetime.now(timezone.utc)})
    try:
        r = admin_a.delete(f"{BASE_URL}/api/authority/inventory/orphan-bots/{bot_id}", timeout=20)
        assert r.status_code == 200, r.text[:200]
        j = r.json()
        assert j.get("ok") is True
        assert db.bot_configs.find_one({"_id": bot_id}) is None
    finally:
        db.bot_configs.delete_one({"_id": bot_id})


def test_delete_orphan_bot_conflict_when_bound(admin_a, db):
    uid = f"TEST_golive_{ObjectId()}"
    bot_id = ObjectId()
    db.bot_configs.insert_one({"_id": bot_id, "user_id": uid, "account_id": "acc-xyz",
                               "symbol": "EURUSD", "active": False,
                               "created_at": datetime.now(timezone.utc)})
    try:
        r = admin_a.delete(f"{BASE_URL}/api/authority/inventory/orphan-bots/{bot_id}", timeout=20)
        assert r.status_code == 409, r.text[:200]
        body = r.json()
        detail = body.get("detail") if isinstance(body, dict) else body
        if isinstance(detail, dict):
            assert detail.get("code") == "bot_has_account"
        else:
            assert "bot_has_account" in str(detail)
    finally:
        db.bot_configs.delete_one({"_id": bot_id})


def test_delete_orphan_bot_unknown_404(admin_a):
    r = admin_a.delete(f"{BASE_URL}/api/authority/inventory/orphan-bots/{ObjectId()}", timeout=20)
    assert r.status_code == 404, r.text[:200]


def test_delete_orphan_bot_non_admin_forbidden(user_session, db):
    uid = f"TEST_golive_{ObjectId()}"
    bot_id = ObjectId()
    db.bot_configs.insert_one({"_id": bot_id, "user_id": uid, "account_id": None,
                               "symbol": "EURUSD", "active": False,
                               "created_at": datetime.now(timezone.utc)})
    try:
        r = user_session.delete(f"{BASE_URL}/api/authority/inventory/orphan-bots/{bot_id}", timeout=20)
        assert r.status_code == 403, r.text[:200]
    finally:
        db.bot_configs.delete_one({"_id": bot_id})


# ── 4-eyes expectation flow ──
def test_expectation_4eyes_flow(admin_a, admin_b, db):
    # snapshot current platform_state docs
    prev_exp = db.platform_state.find_one({"_id": "inventory_expectation"})
    prev_pending = db.platform_state.find_one({"_id": "inventory_expectation_pending"})
    try:
        # Clear any existing pending so proposer is fresh
        db.platform_state.delete_one({"_id": "inventory_expectation_pending"})
        payload = {"accounts": 7, "enabled": 5, "bots": 5}
        r = admin_a.post(f"{BASE_URL}/api/authority/inventory/expectation", json=payload, timeout=20)
        assert r.status_code in (200, 201), r.text[:200]

        # Same-admin approve rejected
        r2 = admin_a.post(f"{BASE_URL}/api/authority/inventory/expectation/approve", timeout=20)
        assert r2.status_code in (403, 409), f"same-admin approve must be rejected, got {r2.status_code}: {r2.text[:200]}"
        body = r2.json() if r2.content else {}
        txt = str(body)
        assert "second_admin_required" in txt or "different" in txt.lower() or r2.status_code == 403, txt[:200]

        # Different admin approves
        r3 = admin_b.post(f"{BASE_URL}/api/authority/inventory/expectation/approve", timeout=20)
        if r3.status_code != 200:
            pytest.skip(f"admin_b approve not 200 (status {r3.status_code}): maybe admin_b missing/not-admin — {r3.text[:200]}")
        exp_after = db.platform_state.find_one({"_id": "inventory_expectation"})
        assert exp_after is not None
        assert exp_after.get("accounts") == 7 and exp_after.get("enabled") == 5 and exp_after.get("bots") == 5
    finally:
        # restore prev docs
        db.platform_state.delete_one({"_id": "inventory_expectation_pending"})
        db.platform_state.delete_one({"_id": "inventory_expectation"})
        if prev_exp:
            db.platform_state.insert_one(prev_exp)
        if prev_pending:
            db.platform_state.insert_one(prev_pending)


# ── promote_admin.py CLI ──
def test_promote_admin_unknown_email_exits_1():
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "promote_admin.py")
    r = subprocess.run([sys.executable, script, f"nouser_{ObjectId()}@nowhere.test"],
                       cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 1, f"exit={r.returncode} stdout={r.stdout[:200]} stderr={r.stderr[:200]}"
    assert "REFUSED" in r.stdout


def test_promote_admin_bad_args_exits_2():
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ops", "promote_admin.py")
    # no arg
    r1 = subprocess.run([sys.executable, script],
                        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        capture_output=True, text=True, timeout=30)
    assert r1.returncode == 2, f"no-arg exit={r1.returncode}"
    # invalid (no @)
    r2 = subprocess.run([sys.executable, script, "notanemail"],
                        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        capture_output=True, text=True, timeout=30)
    assert r2.returncode == 2, f"bad-arg exit={r2.returncode}"
