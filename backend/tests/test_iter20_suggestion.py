"""Iter20 — Safety Blocks "Suggested Config Adjustment" tests.

Verifies:
 - GET /api/safety-blocks/suggestion?days=7
     * <3 blocks → has_suggestion=false
     * ≥3 per_trade_risk_cap → patch.risk_level downshift (high→medium / medium→low / low→low)
     * ≥3 total_open_risk_cap → patch.max_concurrent_trades = max(1, current-1)
     * ≥3 lot_vs_equity_sanity → patch.max_lot_size = round(max(0.01, current*0.7), 2)
     * ≥3 daily_loss_cap | equity_vs_balance_floor | free_margin_floor → patch.active=false (red)
     * ≥3 risk_inputs_present → has_suggestion=true, patch=null
 - POST /api/safety-blocks/apply-suggestion
     * sanitize: drops non-whitelisted keys
     * 400 when patch missing / no allowed keys
     * writes updated_at + last_suggestion_applied_at
     * returns new_config
 - User isolation: A cannot apply on B's account_id (404)
"""
import os
import uuid
import pytest
import requests
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


def _register(email, password):
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/register",
               json={"email": email, "password": password, "name": "iter20",
                     "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), f"Register failed: {r.status_code} {r.text}"
    # newer auth flow requires email verification — mark verified directly
    import asyncio as _aio
    from dotenv import load_dotenv as _ld
    _ld(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

    async def _verify():
        cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
        await cli[os.environ["DB_NAME"]].users.update_one(
            {"email": email.lower()}, {"$set": {"email_verified": True}})
        cli.close()
    _aio.new_event_loop().run_until_complete(_verify())
    r2 = s.post(f"{BASE_URL}/api/auth/login",
                json={"email": email, "password": password}, timeout=15)
    assert r2.status_code == 200, f"Login failed: {r2.status_code} {r2.text}"
    return s


def _me_id(s):
    r = s.get(f"{BASE_URL}/api/auth/me", timeout=10)
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.fixture
def db():
    # Function-scope so each async test uses its own event loop's client
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


@pytest.fixture
def user_a():
    email = f"TEST_iter20_a_{uuid.uuid4().hex[:8]}@example.com"
    return _register(email, "Vx7#Qm2pL9wTzK4e"), email


@pytest.fixture
def user_b():
    email = f"TEST_iter20_b_{uuid.uuid4().hex[:8]}@example.com"
    return _register(email, "Vx7#Qm2pL9wTzK4e"), email


async def _seed_blocks(db, user_id, account_id, code, n=3):
    now = datetime.now(timezone.utc)
    docs = [{
        "user_id": user_id,
        "account_id": account_id,
        "symbol": "XAUUSD",
        "action": "BUY",
        "lot_size": 0.1,
        "blocked_by": code,
        "audit": [{"name": code, "ok": False, "reason": "seed"}],
        "context": {"mode": "live", "equity": 27000.0},
        "blocked_at": (now - timedelta(minutes=i)).isoformat(),
    } for i in range(n)]
    res = await db.safety_blocks.insert_many(docs)
    return res.inserted_ids


async def _set_cfg(db, user_id, account_id, cfg):
    await db.bot_configs.update_one(
        {"user_id": user_id, "account_id": account_id},
        {"$set": {**cfg, "user_id": user_id, "account_id": account_id}},
        upsert=True,
    )


async def _cleanup(db, user_id, account_id, block_ids):
    await db.safety_blocks.delete_many({"_id": {"$in": block_ids}})
    await db.bot_configs.delete_many({"user_id": user_id, "account_id": account_id})


# ---------- Suggestion endpoint ----------
def test_suggestion_no_blocks_returns_false(user_a):
    s, _ = user_a
    r = s.get(f"{BASE_URL}/api/safety-blocks/suggestion?days=7", timeout=10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_suggestion"] is False
    assert "reason" in body


@pytest.mark.asyncio
async def test_suggestion_under_threshold(user_a, db):
    s, _ = user_a
    uid = _me_id(s)
    acct = f"acct_{uid}"
    ids = await _seed_blocks(db, uid, acct, "per_trade_risk_cap", n=2)
    try:
        r = s.get(f"{BASE_URL}/api/safety-blocks/suggestion?days=7", timeout=10)
        assert r.status_code == 200
        assert r.json()["has_suggestion"] is False
    finally:
        await _cleanup(db, uid, acct, ids)


@pytest.mark.asyncio
async def test_suggestion_per_trade_risk_cap_high_to_medium(user_a, db):
    s, _ = user_a
    uid = _me_id(s)
    acct = f"acct_{uid}"
    await _set_cfg(db, uid, acct, {"risk_level": "high", "max_lot_size": 1.0, "max_concurrent_trades": 3})
    ids = await _seed_blocks(db, uid, acct, "per_trade_risk_cap", n=3)
    try:
        r = s.get(f"{BASE_URL}/api/safety-blocks/suggestion?days=7", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["has_suggestion"] is True
        assert body["top_reason"] == "per_trade_risk_cap"
        assert body["count"] == 3
        assert body["severity"] == "amber"
        assert body["patch"] == {"risk_level": "medium"}
        assert body["scope_account_id"] == acct
        assert body["reason_label"]
        assert body["title"]
        assert body["rationale"]
        assert body["preview"]
        assert body["window_days"] == 7
    finally:
        await _cleanup(db, uid, acct, ids)


@pytest.mark.asyncio
async def test_suggestion_per_trade_risk_cap_medium_to_low(user_a, db):
    s, _ = user_a
    uid = _me_id(s)
    acct = f"acct_{uid}"
    await _set_cfg(db, uid, acct, {"risk_level": "medium"})
    ids = await _seed_blocks(db, uid, acct, "per_trade_risk_cap", n=4)
    try:
        body = s.get(f"{BASE_URL}/api/safety-blocks/suggestion?days=7", timeout=10).json()
        assert body["patch"] == {"risk_level": "low"}
    finally:
        await _cleanup(db, uid, acct, ids)


@pytest.mark.asyncio
async def test_suggestion_per_trade_risk_cap_low_stays_low(user_a, db):
    s, _ = user_a
    uid = _me_id(s)
    acct = f"acct_{uid}"
    await _set_cfg(db, uid, acct, {"risk_level": "low"})
    ids = await _seed_blocks(db, uid, acct, "per_trade_risk_cap", n=3)
    try:
        body = s.get(f"{BASE_URL}/api/safety-blocks/suggestion?days=7", timeout=10).json()
        assert body["patch"] == {"risk_level": "low"}
    finally:
        await _cleanup(db, uid, acct, ids)


@pytest.mark.asyncio
async def test_suggestion_total_open_risk_cap(user_a, db):
    s, _ = user_a
    uid = _me_id(s)
    acct = f"acct_{uid}"
    await _set_cfg(db, uid, acct, {"max_concurrent_trades": 5})
    ids = await _seed_blocks(db, uid, acct, "total_open_risk_cap", n=3)
    try:
        body = s.get(f"{BASE_URL}/api/safety-blocks/suggestion?days=7", timeout=10).json()
        assert body["has_suggestion"] is True
        assert body["severity"] == "amber"
        assert body["patch"] == {"max_concurrent_trades": 4}
    finally:
        await _cleanup(db, uid, acct, ids)


@pytest.mark.asyncio
async def test_suggestion_total_open_risk_cap_floors_at_1(user_a, db):
    s, _ = user_a
    uid = _me_id(s)
    acct = f"acct_{uid}"
    await _set_cfg(db, uid, acct, {"max_concurrent_trades": 1})
    ids = await _seed_blocks(db, uid, acct, "total_open_risk_cap", n=3)
    try:
        body = s.get(f"{BASE_URL}/api/safety-blocks/suggestion?days=7", timeout=10).json()
        assert body["patch"] == {"max_concurrent_trades": 1}  # max(1, 0)
    finally:
        await _cleanup(db, uid, acct, ids)


@pytest.mark.asyncio
async def test_suggestion_lot_vs_equity_sanity(user_a, db):
    s, _ = user_a
    uid = _me_id(s)
    acct = f"acct_{uid}"
    await _set_cfg(db, uid, acct, {"max_lot_size": 1.0})
    ids = await _seed_blocks(db, uid, acct, "lot_vs_equity_sanity", n=3)
    try:
        body = s.get(f"{BASE_URL}/api/safety-blocks/suggestion?days=7", timeout=10).json()
        assert body["has_suggestion"] is True
        assert body["severity"] == "amber"
        assert body["patch"] == {"max_lot_size": 0.7}  # round(1.0*0.7, 2)
    finally:
        await _cleanup(db, uid, acct, ids)


@pytest.mark.parametrize("code", ["daily_loss_cap", "equity_vs_balance_floor", "free_margin_floor"])
@pytest.mark.asyncio
async def test_suggestion_red_pause(user_a, db, code):
    s, _ = user_a
    uid = _me_id(s)
    acct = f"acct_{uid}"
    await _set_cfg(db, uid, acct, {"active": True})
    ids = await _seed_blocks(db, uid, acct, code, n=3)
    try:
        body = s.get(f"{BASE_URL}/api/safety-blocks/suggestion?days=7", timeout=10).json()
        assert body["has_suggestion"] is True
        assert body["severity"] == "red"
        assert body["patch"] == {"active": False}
    finally:
        await _cleanup(db, uid, acct, ids)


@pytest.mark.asyncio
async def test_suggestion_risk_inputs_present_manual(user_a, db):
    s, _ = user_a
    uid = _me_id(s)
    acct = f"acct_{uid}"
    await _set_cfg(db, uid, acct, {})
    ids = await _seed_blocks(db, uid, acct, "risk_inputs_present", n=3)
    try:
        body = s.get(f"{BASE_URL}/api/safety-blocks/suggestion?days=7", timeout=10).json()
        assert body["has_suggestion"] is True
        assert body["patch"] is None  # manual review
        assert body["top_reason"] == "risk_inputs_present"
    finally:
        await _cleanup(db, uid, acct, ids)


def test_suggestion_requires_auth():
    r = requests.get(f"{BASE_URL}/api/safety-blocks/suggestion", timeout=10)
    assert r.status_code in (401, 403)


# ---------- Apply endpoint ----------
@pytest.mark.asyncio
async def test_apply_suggestion_400_no_patch(user_a, db):
    s, _ = user_a
    uid = _me_id(s)
    acct = f"acct_{uid}"
    await _set_cfg(db, uid, acct, {"risk_level": "high"})
    try:
        r = s.post(f"{BASE_URL}/api/safety-blocks/apply-suggestion", json={}, timeout=10)
        assert r.status_code == 400
    finally:
        await _cleanup(db, uid, acct, [])


@pytest.mark.asyncio
async def test_apply_suggestion_400_when_no_allowed_keys(user_a, db):
    s, _ = user_a
    uid = _me_id(s)
    acct = f"acct_{uid}"
    await _set_cfg(db, uid, acct, {"risk_level": "high"})
    try:
        # only contains non-whitelisted keys
        r = s.post(f"{BASE_URL}/api/safety-blocks/apply-suggestion",
                   json={"patch": {"evil": "x", "other": 1}}, timeout=10)
        assert r.status_code == 400
    finally:
        await _cleanup(db, uid, acct, [])


@pytest.mark.asyncio
async def test_apply_suggestion_sanitizes_and_persists(user_a, db):
    s, _ = user_a
    uid = _me_id(s)
    try:
        # mixed patch: risk_level allowed, "secret" dropped
        r = s.post(f"{BASE_URL}/api/safety-blocks/apply-suggestion",
                   json={"patch": {"risk_level": "medium", "secret_admin_field": "x"}}, timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] is True
        assert "applied_at" in body
        assert body["patch"].get("risk_level") == "medium"
        assert "secret_admin_field" not in body["patch"]
        assert "updated_at" in body["patch"]
        assert "last_suggestion_applied_at" in body["patch"]
        assert body["new_config"]["risk_level"] == "medium"

        # verify persisted in DB (the bot_config that the API targeted)
        cfg = await db.bot_configs.find_one({"user_id": uid})
        assert cfg is not None
        assert cfg["risk_level"] == "medium"
        assert "updated_at" in cfg
        assert "last_suggestion_applied_at" in cfg
        assert "secret_admin_field" not in cfg
    finally:
        await db.bot_configs.delete_many({"user_id": uid})


@pytest.mark.asyncio
async def test_apply_suggestion_404_when_no_bot_config_for_account(user_a, user_b, db):
    """If user provides scope_account_id that doesn't belong to them → 404."""
    s_a, _ = user_a
    fake_acct = str(ObjectId())  # not owned by anyone
    r = s_a.post(f"{BASE_URL}/api/safety-blocks/apply-suggestion",
                 json={"patch": {"risk_level": "low"}, "scope_account_id": fake_acct},
                 timeout=10)
    # Either 404 (account not found) — server checks ownership first
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_apply_suggestion_user_isolation(user_a, user_b, db):
    """User A cannot apply a patch by passing User B's account_id."""
    s_a, _ = user_a
    s_b, _ = user_b
    uid_a = _me_id(s_a)
    uid_b = _me_id(s_b)

    # Create a real account doc owned by B (ObjectId format)
    b_acct = await db.accounts.insert_one(
        {"user_id": uid_b, "name": "B-acct",
         "bridge_token": f"test-{uuid.uuid4().hex}"})
    b_acct_id = str(b_acct.inserted_id)
    await _set_cfg(db, uid_b, b_acct_id, {"risk_level": "high"})

    try:
        # A tries to apply with B's account_id → must be 404
        r = s_a.post(f"{BASE_URL}/api/safety-blocks/apply-suggestion",
                     json={"patch": {"risk_level": "low"}, "scope_account_id": b_acct_id},
                     timeout=10)
        assert r.status_code == 404, f"User A should get 404 on B's acct, got {r.status_code}: {r.text}"

        # B's config must be untouched
        cfg = await db.bot_configs.find_one({"user_id": uid_b, "account_id": b_acct_id})
        assert cfg["risk_level"] == "high"
    finally:
        await db.accounts.delete_one({"_id": ObjectId(b_acct_id)})
        await db.bot_configs.delete_many({"account_id": b_acct_id})


def test_apply_suggestion_requires_auth():
    r = requests.post(f"{BASE_URL}/api/safety-blocks/apply-suggestion",
                      json={"patch": {"risk_level": "low"}}, timeout=10)
    assert r.status_code in (401, 403)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
