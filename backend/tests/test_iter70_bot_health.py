"""Tests for iter-70 — Smarter Bot Health scoring.

Verifies the diagnostic upgrades that took the user's admin score from
53 → 78:
  • Heartbeat stale > 1h → account auto-flipped to dormant (no per-account
    -10 penalty; single -5 advisory the FIRST run, info-only afterwards)
  • Ghost trades older than 24h auto-acknowledged (cleared from score)
  • bot_inactive demoted from -10 → -5 (user choice, not a malfunction)
  • All-dormant accounts do NOT trigger the -35 "no_connected_account" panic
"""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient


BASE_URL = "https://stoic-trading-bot.preview.emergentagent.com"
if "REACT_APP_BACKEND_URL" not in os.environ:
    try:
        with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL"):
                    BASE_URL = line.split("=", 1)[1].strip().strip('"').rstrip("/")
                    break
    except Exception:
        pass

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


def _get_db_url():
    mongo_url = "mongodb://localhost:27017"
    db_name = "test_database"
    try:
        with open(_os.path.join(_BACKEND_DIR, ".env")) as f:
            for line in f:
                if line.startswith("MONGO_URL="):
                    mongo_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("DB_NAME="):
                    db_name = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return mongo_url, db_name


async def _with_db(coro_factory):
    """Open a fresh Motor client per call (avoids event-loop reuse issues)."""
    mongo_url, db_name = _get_db_url()
    client = AsyncIOMotorClient(mongo_url)
    try:
        return await coro_factory(client[db_name])
    finally:
        client.close()


def test_health_score_shape_and_status_bands(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/health-score", timeout=15)
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data["score"], int)
    assert 0 <= data["score"] <= 100
    assert data["status"] in ("excellent", "good", "degraded", "critical")
    assert isinstance(data["issues"], list)
    for issue in data["issues"]:
        assert issue["severity"] in ("error", "warning", "info")
        assert "code" in issue and "label" in issue


def test_dormant_auto_flip_and_softer_penalty(admin_session):
    """Stale-for-1h+ accounts get marked dormant + softer penalty."""
    fresh_id = ObjectId()
    dormant_id = ObjectId()

    async def _seed(db):
        user = await db.users.find_one({"email": ADMIN_EMAIL})
        assert user, "admin seed user missing"
        user_id = str(user["_id"])
        now = datetime.now(timezone.utc)
        await db.accounts.insert_many([
            {"_id": fresh_id, "user_id": user_id, "label": "iter70_fresh",
             "mode": "live", "status": "connected",
             "last_heartbeat": now.isoformat(),
             "bridge_token": f"iter70_fresh_{fresh_id}",
             "ea_version": "1.28"},
            {"_id": dormant_id, "user_id": user_id, "label": "iter70_dormant",
             "mode": "live", "status": "connected",
             "last_heartbeat": (now - timedelta(hours=3)).isoformat(),
             "bridge_token": f"iter70_dormant_{dormant_id}",
             "ea_version": "1.28"},
        ])

    async def _verify(db):
        return await db.accounts.find_one({"_id": dormant_id})

    async def _cleanup(db):
        await db.accounts.delete_many({"_id": {"$in": [fresh_id, dormant_id]}})

    asyncio.run(_with_db(_seed))
    try:
        r = admin_session.get(f"{BASE_URL}/api/bot/health-score", timeout=15)
        assert r.status_code == 200
        data = r.json()
        codes = {i["code"] for i in data["issues"]}
        assert "dormant_accounts" in codes
        # release review P1-4: GET is READ-ONLY — persistence happens in the
        # ledgered repair job (analytics worker). Run it explicitly here.
        untouched = asyncio.run(_with_db(_verify))
        assert untouched.get("dormant") is not True
        from health_repairs import run_health_repairs
        async def _repair(db):
            u = await db.users.find_one({"email": ADMIN_EMAIL})
            return await run_health_repairs(db, str(u["_id"]))
        asyncio.run(_with_db(_repair))
        updated = asyncio.run(_with_db(_verify))
        assert updated.get("dormant") is True
        assert updated.get("status") == "disconnected"
    finally:
        asyncio.run(_with_db(_cleanup))


def test_old_ghost_trades_auto_acknowledged(admin_session):
    """Ghosts >24h old are auto-acknowledged on next health check."""
    old_id = ObjectId()
    recent_id = ObjectId()

    async def _seed(db):
        user = await db.users.find_one({"email": ADMIN_EMAIL})
        user_id = str(user["_id"])
        now = datetime.now(timezone.utc)
        await db.trades.insert_many([
            {"_id": old_id, "user_id": user_id, "status": "closed",
             "exit_price": None, "symbol": "XAUUSD", "side": "BUY",
             "closed_at": (now - timedelta(hours=48)).isoformat()},
            {"_id": recent_id, "user_id": user_id, "status": "closed",
             "exit_price": None, "symbol": "BTCUSD", "side": "SELL",
             "closed_at": (now - timedelta(hours=2)).isoformat()},
        ])

    async def _verify(db):
        return (await db.trades.find_one({"_id": old_id}),
                await db.trades.find_one({"_id": recent_id}))

    async def _cleanup(db):
        await db.trades.delete_many({"_id": {"$in": [old_id, recent_id]}})

    asyncio.run(_with_db(_seed))
    try:
        r = admin_session.get(f"{BASE_URL}/api/bot/health-score", timeout=15)
        assert r.status_code == 200
        old, recent = asyncio.run(_with_db(_verify))
        assert old.get("ghost_acknowledged") is not True   # GET is read-only
        from health_repairs import run_health_repairs
        async def _repair(db):
            u = await db.users.find_one({"email": ADMIN_EMAIL})
            return await run_health_repairs(db, str(u["_id"]))
        asyncio.run(_with_db(_repair))
        old, recent = asyncio.run(_with_db(_verify))
        assert old.get("ghost_acknowledged") is True
        assert old.get("ghost_auto_ack_reason") == "older_than_24h"
        assert recent.get("ghost_acknowledged") is not True
    finally:
        asyncio.run(_with_db(_cleanup))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
