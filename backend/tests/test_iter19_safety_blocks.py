from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""Iter19 — Safety Blocks page API tests.

Verifies the user-facing /api/safety-blocks/* endpoints:
 - GET /list with pagination + window bounds + user isolation
 - GET /stats with by_reason, by_day (days+1 zero-filled), thresholds
 - GET /{block_id}: 400 on bad ObjectId, 404 on other-user's block
"""
import os
import uuid
import pytest
import requests
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
pass  # ADMIN_EMAIL comes from live_target
pass  # ADMIN_PASSWORD comes from live_target
def _login(email, password):
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, f"Login failed: {r.status_code} {r.text}"
    return s


def _register(email, password):
    from helpers import register_and_login
    return register_and_login(email, password, name="iter19")


@pytest.fixture(scope="module")
def admin_session():
    return _login(ADMIN_EMAIL, ADMIN_PASSWORD)


@pytest.fixture(scope="module")
def user_a():
    email = f"TEST_iter19_a_{uuid.uuid4().hex[:8]}@example.com"
    return _register(email, "Vx7#Qm2pL9wTzK4e"), email


@pytest.fixture(scope="module")
def user_b():
    email = f"TEST_iter19_b_{uuid.uuid4().hex[:8]}@example.com"
    return _register(email, "Vx7#Qm2pL9wTzK4e"), email


@pytest.fixture(scope="module")
def db():
    # Connect to mongo directly to seed safety_blocks
    from motor.motor_asyncio import AsyncIOMotorClient
    mongo_url = os.environ["MONGO_URL"]
    db_name = os.environ["DB_NAME"]
    client = AsyncIOMotorClient(mongo_url)
    return client[db_name]


def _me_id(session):
    r = session.get(f"{BASE_URL}/api/auth/me", timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ---------- /list ----------
def test_list_requires_auth():
    r = requests.get(f"{BASE_URL}/api/safety-blocks/list", timeout=10)
    assert r.status_code in (401, 403)


def test_list_default_params(user_a):
    s, _ = user_a
    r = s.get(f"{BASE_URL}/api/safety-blocks/list", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "blocks" in body and isinstance(body["blocks"], list)
    assert body["window_days"] == 7
    assert "total_in_window" in body
    assert isinstance(body["total_in_window"], int)


def test_list_pagination_bounds(user_a):
    s, _ = user_a
    # limit > 200 must be rejected
    r = s.get(f"{BASE_URL}/api/safety-blocks/list?limit=500", timeout=10)
    assert r.status_code == 422
    # limit < 1 rejected
    r = s.get(f"{BASE_URL}/api/safety-blocks/list?limit=0", timeout=10)
    assert r.status_code == 422
    # days > 90 rejected
    r = s.get(f"{BASE_URL}/api/safety-blocks/list?days=120", timeout=10)
    assert r.status_code == 422
    # days < 1 rejected
    r = s.get(f"{BASE_URL}/api/safety-blocks/list?days=0", timeout=10)
    assert r.status_code == 422


def test_list_custom_window(user_a):
    s, _ = user_a
    r = s.get(f"{BASE_URL}/api/safety-blocks/list?days=30&limit=10", timeout=10)
    assert r.status_code == 200
    assert r.json()["window_days"] == 30


# ---------- /stats ----------
def test_stats_shape_and_zero_fill(user_a):
    s, _ = user_a
    r = s.get(f"{BASE_URL}/api/safety-blocks/stats?days=7", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("by_reason", "by_day", "total", "thresholds", "window_days"):
        assert k in body, f"missing {k}"
    assert body["window_days"] == 7
    # by_day must be days+1 zero-filled (continuous)
    assert len(body["by_day"]) == 8, f"by_day expected 8 entries, got {len(body['by_day'])}"
    for entry in body["by_day"]:
        assert "date" in entry and "count" in entry
    # thresholds is a dict with guardian config keys
    assert isinstance(body["thresholds"], dict)
    assert len(body["thresholds"]) >= 4


def test_stats_days_30_returns_31_entries(user_a):
    s, _ = user_a
    r = s.get(f"{BASE_URL}/api/safety-blocks/stats?days=30", timeout=10)
    assert r.status_code == 200
    assert len(r.json()["by_day"]) == 31


def test_stats_requires_auth():
    r = requests.get(f"{BASE_URL}/api/safety-blocks/stats", timeout=10)
    assert r.status_code in (401, 403)


# ---------- /{block_id} ----------
def test_block_detail_invalid_objectid(user_a):
    s, _ = user_a
    r = s.get(f"{BASE_URL}/api/safety-blocks/not-an-objectid", timeout=10)
    # Iter22 hardening normalized invalid-ObjectId → 404 across all resources
    assert r.status_code == 404


def test_block_detail_nonexistent(user_a):
    s, _ = user_a
    fake_id = str(ObjectId())
    r = s.get(f"{BASE_URL}/api/safety-blocks/{fake_id}", timeout=10)
    assert r.status_code == 404


# ---------- seeded-data tests: list ordering, totals, isolation ----------
@pytest.mark.asyncio
async def test_seed_and_verify_user_isolation(user_a, user_b, db):
    s_a, _ = user_a
    s_b, _ = user_b
    uid_a = _me_id(s_a)
    uid_b = _me_id(s_b)

    now = datetime.now(timezone.utc)
    docs_a = []
    for i, code in enumerate(["per_trade_risk_cap", "daily_loss_cap", "free_margin_floor"]):
        doc = {
            "user_id": uid_a,
            "account_id": f"acc_{uid_a}",
            "symbol": "XAUUSD",
            "action": "BUY" if i % 2 == 0 else "SELL",
            "lot_size": 0.10 + i * 0.05,
            "blocked_by": code,
            "audit": [{"name": code, "ok": False, "reason": "test"}],
            "context": {"mode": "live", "equity": 1000.0, "balance": 1200.0},
            "blocked_at": (now - timedelta(hours=i)).isoformat(),
        }
        docs_a.append(doc)
    res_a = await db.safety_blocks.insert_many(docs_a)
    a_ids = [str(_id) for _id in res_a.inserted_ids]

    # one block for user B
    res_b = await db.safety_blocks.insert_one({
        "user_id": uid_b, "account_id": "acc_b", "symbol": "EURUSD",
        "action": "BUY", "lot_size": 0.05, "blocked_by": "equity_known",
        "audit": [{"name": "equity_known", "ok": False}],
        "context": {"mode": "live"}, "blocked_at": now.isoformat(),
    })
    b_id = str(res_b.inserted_id)

    try:
        # User A sees only their 3 blocks
        r = s_a.get(f"{BASE_URL}/api/safety-blocks/list?days=7", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["total_in_window"] == 3, f"expected 3 got {body['total_in_window']}"
        # newest first
        ts = [b["blocked_at"] for b in body["blocks"]]
        assert ts == sorted(ts, reverse=True), "Not sorted newest-first"
        # reason_label populated
        for b in body["blocks"]:
            assert b.get("reason_label") and len(b["reason_label"]) > 0
            assert "id" in b and b["id"]
            assert "_id" not in b  # mongo _id must be stripped

        # Stats reflect 3 blocks across reasons
        r = s_a.get(f"{BASE_URL}/api/safety-blocks/stats?days=7", timeout=10)
        assert r.status_code == 200
        stats = r.json()
        assert stats["total"] == 3
        codes = sorted([row["blocked_by"] for row in stats["by_reason"]])
        assert codes == sorted(["per_trade_risk_cap", "daily_loss_cap", "free_margin_floor"])

        # Detail of A's block works
        r = s_a.get(f"{BASE_URL}/api/safety-blocks/{a_ids[0]}", timeout=10)
        assert r.status_code == 200
        detail = r.json()
        assert detail["id"] == a_ids[0]
        assert detail["audit"] and detail["context"]

        # USER ISOLATION: User A cannot access User B's block
        r = s_a.get(f"{BASE_URL}/api/safety-blocks/{b_id}", timeout=10)
        assert r.status_code == 404, f"User A should get 404 on B's block, got {r.status_code}"

        # User B sees only their 1 block
        r = s_b.get(f"{BASE_URL}/api/safety-blocks/list?days=7", timeout=10)
        assert r.status_code == 200
        assert r.json()["total_in_window"] == 1
    finally:
        # cleanup
        await db.safety_blocks.delete_many({"_id": {"$in": res_a.inserted_ids + [res_b.inserted_id]}})


# ---------- Regression: diagnostic still works ----------
def test_diagnostic_admin(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/diagnostic/run", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    # Has safety guardian checks
    text = str(body)
    assert "Safety Guardian" in text or "safety" in text.lower()


def test_diagnostic_nonadmin_403(user_a):
    s, _ = user_a
    r = s.get(f"{BASE_URL}/api/diagnostic/run", timeout=15)
    assert r.status_code == 403


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
