"""iter-91 · Bot start clears stale panic/trip flags + startup auto-heal.

Two surfaces:
  1. POST /api/bot/start MUST clear `tripped_at` + `tripped_reason` when
     re-enabling a panic-tripped bot. Without this, the dashboard keeps
     showing "PANIC LOCK — all trading halted by user/admin" forever even
     though the bot is happily trading.
  2. `seed_admin()` runs an idempotent auto-heal pass at every startup that
     clears stale `tripped_*` fields on any `active=True` bot — so users
     don't have to Stop/Start every account after deploying the fix.
"""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import sys
import uuid
from datetime import datetime, timezone

import requests
from bson import ObjectId
from pymongo import MongoClient
from ea_version import current_ea_version
from live_target import require_live_base_url

_BACKEND_DIR = _BACKEND_DIR
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
with open(f"{_BACKEND_DIR}/.env") as _f:
    for _ln in _f:
        if "=" in _ln and not _ln.lstrip().startswith("#"):
            _k, _v = _ln.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

BASE_URL = require_live_base_url()
TIMEOUT = 30


def _mongo():
    return MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


def _register_verified() -> tuple[str, requests.Session]:
    suffix = uuid.uuid4().hex[:10]
    email, pw = f"iter91_{suffix}@example.com", "Gy6#Vb3kM9zRnD2s"
    r = requests.post(f"{BASE_URL}/api/auth/register",
                      json={"email": email, "password": pw,
                            "name": f"iter91-{suffix}", "terms_agreed": True},
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
    return uid, s


def test_bot_start_clears_stale_panic_flags():
    uid, sess = _register_verified()
    db = _mongo()

    # Create an account + tripped bot_config
    r = sess.post(f"{BASE_URL}/api/accounts", json={
        "label": f"iter91-{uuid.uuid4().hex[:6]}",
        "broker": "STARTRADER", "server": "T-Demo",
        "account_number": f"iter91-{uuid.uuid4().hex[:8]}",
        "account_type": "demo", "base_currency": "USD", "mode": "live",
    }, timeout=TIMEOUT)
    aid = r.json()["id"]

    # Activation readiness (audit E10) now requires a connected EA — make the
    # account look healthy so this test can exercise panic-flag clearing.
    db.accounts.update_one({"_id": ObjectId(aid)}, {"$set": {
        "status": "connected", "ea_version": current_ea_version(),
        "equity": 10000.0,
        "balance": 10000.0,
        "last_heartbeat": datetime.now(timezone.utc).isoformat()}})

    cfg_id = ObjectId()
    db.bot_configs.insert_one({
        "_id": cfg_id,
        "user_id": uid,
        "account_id": aid,
        "active": False,
        "tripped_at": datetime.now(timezone.utc).isoformat(),
        "tripped_reason": "PANIC LOCK — all trading halted by user/admin",
    })

    try:
        # Re-enable via /api/bot/start
        r = sess.post(f"{BASE_URL}/api/bot/start?account_id={aid}", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        # tripped fields must be gone
        cfg = db.bot_configs.find_one({"_id": cfg_id})
        assert cfg["active"] is True, "bot must be active"
        assert "tripped_at" not in cfg, f"tripped_at not cleared: {cfg}"
        assert "tripped_reason" not in cfg, f"tripped_reason not cleared: {cfg}"
    finally:
        db.bot_configs.delete_one({"_id": cfg_id})
        db.accounts.delete_one({"_id": ObjectId(aid)})
        db.users.delete_one({"_id": ObjectId(uid)})


def test_seed_admin_autoheal_clears_stale_flags_on_active_bots():
    """The startup hook self-heals any pre-existing stale tripped fields
    on `active=True` bots — so existing users don't have to toggle Stop/Start
    on every account after the fix is deployed."""
    import asyncio
    from seed import ensure_indexes

    uid, _ = _register_verified()
    db = _mongo()
    aid_active = str(ObjectId())   # synthetic — only need a distinct account_id
    aid_inactive = str(ObjectId())
    cfg_id = ObjectId()
    # Stale-but-active: classic post-bug state
    db.bot_configs.insert_one({
        "_id": cfg_id,
        "user_id": uid,
        "account_id": aid_active,
        "active": True,
        "tripped_at": "2026-06-28T06:31:47.693660+00:00",
        "tripped_reason": "PANIC LOCK — all trading halted by user/admin",
    })
    # Sanity baseline — actively-disabled bot must keep its trip flags
    inactive_id = ObjectId()
    db.bot_configs.insert_one({
        "_id": inactive_id,
        "user_id": uid,
        "account_id": aid_inactive,
        "active": False,
        "tripped_at": "2026-06-28T06:31:47.693660+00:00",
        "tripped_reason": "PANIC LOCK — all trading halted by user/admin",
    })

    try:
        from conftest import run_async
        run_async(ensure_indexes())
        healed = db.bot_configs.find_one({"_id": cfg_id})
        assert "tripped_at" not in healed, f"stale flag not healed: {healed}"
        assert "tripped_reason" not in healed
        # The actively-disabled one must be preserved
        kept = db.bot_configs.find_one({"_id": inactive_id})
        assert "tripped_reason" in kept, "actively-disabled bot was wrongly healed"
    finally:
        db.bot_configs.delete_many({"_id": {"$in": [cfg_id, inactive_id]}})
        db.users.delete_one({"_id": ObjectId(uid)})


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
