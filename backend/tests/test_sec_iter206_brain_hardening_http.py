"""iter-206 SEC-001/002/003 — HTTP verification against preview URL.

Covers:
- SEC-003 · GET /api/brain/costs with regex metacharacters must return 200 (never 500).
- SEC-002 · GET /api/brain/degraded must NOT leak last_error to non-admins;
             admins DO see last_error.
- SEC-001 · POST /api/brain/challenger/{model_id}/qualify:
             404 on nonexistent valid ObjectId, 429 wiring exercised via
             the pure-window unit test (already 5/5 pass in
             test_sec_iter206_brain_hardening.py); we additionally verify
             the endpoint doesn't 500 and that the 404 branch is intact.
- Tenant isolation regression · GET /api/brain/decisions returns only
             the caller's decisions.
"""
import os
import time
from urllib.parse import quote

import pytest
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL",
                          "https://stoic-trading-bot.preview.emergentagent.com"
                          ).rstrip("/")
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "ai_trading_bot")

SECRET_STR = "SECRET-internal-detail-xyz"
TEST_EMAIL = f"TEST_sec206_{int(time.time())}@example.com"
TEST_PW = f"Zq7$sec206-{int(time.time())}-unique"


# ─────────────────────── helpers ───────────────────────

def _login(session: requests.Session, email: str, pw: str):
    r = session.post(f"{BASE_URL}/api/auth/login",
                     json={"email": email, "password": pw}, timeout=15)
    assert r.status_code == 200, f"login failed for {email}: {r.status_code} {r.text}"
    session.get(f"{BASE_URL}/api/auth/csrf", timeout=10)
    session.headers["X-CSRF-Token"] = session.cookies.get("csrf_token", "")


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    _login(s, "admin@stoicaibot.com", "admin123")
    return s


@pytest.fixture(scope="module")
def mongo_db():
    client = AsyncIOMotorClient(MONGO_URL)
    return client[DB_NAME]


@pytest.fixture(scope="module")
def test_user(mongo_db):
    """Register a throwaway user, flip email_verified in Mongo, login."""
    import asyncio
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/register",
               json={"email": TEST_EMAIL, "password": TEST_PW,
                     "name": "sec206 tester", "terms_agreed": True},
               timeout=15)
    assert r.status_code in (200, 201), f"register: {r.status_code} {r.text}"

    async def _verify():
        await mongo_db.users.update_one(
            {"email": TEST_EMAIL.lower()},
            {"$set": {"email_verified": True}})
        return await mongo_db.users.find_one({"email": TEST_EMAIL.lower()})

    user_doc = asyncio.get_event_loop().run_until_complete(_verify())
    assert user_doc, "test user missing after register"

    _login(s, TEST_EMAIL, TEST_PW)
    s.test_email = TEST_EMAIL
    s.test_uid = user_doc.get("id") or str(user_doc.get("_id"))

    yield s

    async def _cleanup():
        await mongo_db.users.delete_many({"email": TEST_EMAIL.lower()})
    asyncio.get_event_loop().run_until_complete(_cleanup())


# ───────────────────── SEC-003 ─────────────────────

@pytest.mark.parametrize("payload", ["(XAU[", ".*", ".*(["])
def test_sec003_costs_survives_regex_payload(admin_session, payload):
    url = f"{BASE_URL}/api/brain/costs?symbol={quote(payload, safe='')}"
    r = admin_session.get(url, timeout=15)
    assert r.status_code == 200, f"expected 200 got {r.status_code} body={r.text[:300]}"
    body = r.json()
    assert "cost_r" in body, f"cost_r missing: {body}"
    assert body["cost_r"] >= 0.03, f"cost_r floor violated: {body['cost_r']}"


# ───────────────────── SEC-002 ─────────────────────

@pytest.fixture
def failing_subsystem_doc(mongo_db):
    """Seed intelligence_health with a failing meta_decision doc that
    carries a distinctive secret string, then remove after test."""
    import asyncio
    from datetime import datetime, timezone

    async def _seed():
        await mongo_db.intelligence_health.replace_one(
            {"_id": "meta_decision"},
            {"_id": "meta_decision",
             "ok": False,
             "consecutive_failures": 2,
             "last_error": SECRET_STR,
             "last_failure_at": datetime.now(timezone.utc)},
            upsert=True)
    asyncio.get_event_loop().run_until_complete(_seed())
    yield

    async def _clean():
        await mongo_db.intelligence_health.delete_one({"_id": "meta_decision"})
    asyncio.get_event_loop().run_until_complete(_clean())


def test_sec002_degraded_non_admin_scrubs_last_error(
        test_user, failing_subsystem_doc):
    r = test_user.get(f"{BASE_URL}/api/brain/degraded", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    # secret must not appear anywhere in the payload for non-admin
    raw = r.text
    assert SECRET_STR not in raw, f"non-admin leaked secret: {raw[:400]}"
    subs = body.get("subsystems", {})
    if "meta_decision" in subs:
        s = subs["meta_decision"]
        assert "last_error" not in s, f"last_error present for non-admin: {s}"
        # structural fields still present
        assert "ok" in s
        assert "policy" in s or "failing" in s


def test_sec002_degraded_admin_sees_last_error(
        admin_session, failing_subsystem_doc):
    r = admin_session.get(f"{BASE_URL}/api/brain/degraded", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    subs = body.get("subsystems", {})
    assert "meta_decision" in subs, f"meta_decision subsystem missing: {subs}"
    s = subs["meta_decision"]
    assert "last_error" in s, f"admin missing last_error: {s}"
    assert SECRET_STR in s["last_error"], s["last_error"]


# ───────────────────── SEC-001 ─────────────────────

def test_sec001_qualify_nonexistent_model_returns_404(test_user):
    """A valid but non-existing ObjectId → 404 (not 500)."""
    fake = str(ObjectId())
    r = test_user.post(
        f"{BASE_URL}/api/brain/challenger/{fake}/qualify", timeout=15)
    assert r.status_code == 404, f"expected 404 got {r.status_code} body={r.text[:300]}"


def test_sec001_qualify_bad_model_id_returns_4xx(test_user):
    """Malformed model_id → 4xx (not 500)."""
    r = test_user.post(
        f"{BASE_URL}/api/brain/challenger/not-an-objectid/qualify",
        timeout=15)
    assert 400 <= r.status_code < 500, f"expected 4xx got {r.status_code}: {r.text[:200]}"


# ───────────────────── Tenant isolation regression ─────────────────────

def test_tenant_iso_decisions_scoped_to_caller(test_user):
    r = test_user.get(f"{BASE_URL}/api/brain/decisions", timeout=15)
    # endpoint may 200 with empty list; some builds route through
    # /api/brain/meta/recent — accept 200 or 404
    assert r.status_code in (200, 404), r.text
    if r.status_code == 200:
        body = r.json()
        # any list-valued key should be empty for the fresh user
        for v in body.values() if isinstance(body, dict) else []:
            if isinstance(v, list):
                for row in v:
                    if isinstance(row, dict) and "user_id" in row:
                        assert row["user_id"] == test_user.test_uid, row


def test_tenant_iso_admin_decision_404_for_non_admin(test_user, admin_session,
                                                    mongo_db):
    """Insert a meta_decision doc owned by admin, then verify the non-admin
    user cannot fetch it by id (endpoint returns 404 or scoped empty)."""
    import asyncio
    from datetime import datetime, timezone
    admin_dec_id = f"TEST_sec206_admin_dec_{int(time.time())}"

    async def _seed():
        # look up admin id
        adm = await mongo_db.users.find_one({"email": "admin@stoicaibot.com"})
        aid = adm.get("id") or str(adm.get("_id"))
        await mongo_db.meta_decisions.insert_one({
            "decision_id": admin_dec_id,
            "user_id": aid,
            "at": datetime.now(timezone.utc).isoformat(),
            "note": "TEST_sec206 seed"})
        return aid
    asyncio.get_event_loop().run_until_complete(_seed())

    try:
        # if a by-id endpoint exists, expect 404 for non-admin
        r = test_user.get(
            f"{BASE_URL}/api/brain/decisions/{admin_dec_id}", timeout=15)
        assert r.status_code in (404, 403, 405), \
            f"non-admin got {r.status_code} for admin decision: {r.text[:200]}"
    finally:
        async def _clean():
            await mongo_db.meta_decisions.delete_many(
                {"decision_id": admin_dec_id})
        asyncio.get_event_loop().run_until_complete(_clean())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
