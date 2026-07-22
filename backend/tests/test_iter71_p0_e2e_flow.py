"""iter-71 · E2E verification of the P0-1 unresolved -> open -> replay lifecycle.

Uses an EXISTING account with bridge_token so we never leak account docs (a
prior test leaked an account without a 'label' field which broke iter24e).
All inserted trade docs are cleaned up in a finally block.
"""
import os
import asyncio
from pathlib import Path

import pytest
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

_BACKEND_DIR = Path(__file__).resolve().parent.parent
load_dotenv(_BACKEND_DIR / ".env")

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")


@pytest.fixture(scope="module")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="module")
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return client[os.environ["DB_NAME"]]


@pytest.fixture(scope="module")
def admin_cookies():
    r = requests.post(f"{BASE_URL}/api/auth/login",
                      json={"email": "admin@trading.bot", "password": "admin123"},
                      timeout=15)
    assert r.status_code == 200, r.text
    return r.cookies


async def _find_bridged_account(db):
    async for a in db.accounts.find({"bridge_token": {"$exists": True, "$ne": None}}):
        if a.get("bridge_token"):
            return a
    return None


@pytest.mark.asyncio
async def test_p0_unresolved_then_open_then_replay(db):
    acc = await _find_bridged_account(db)
    if not acc:
        pytest.skip("No account with bridge_token in DB")

    trade_doc = {
        "user_id": acc["user_id"],
        "account_id": str(acc["_id"]),
        "symbol": "XAUUSD",
        "base_symbol": "XAUUSD",
        "action": "buy",
        "lot_size": 1.00,
        "status": "pending",
        "origin": "auto",
        "submission_state": "dispatched",
        "_dispatched_at": "2026-01-01T00:00:00+00:00",
        "_iter71_marker": True,
    }
    inserted = await db.trades.insert_one(trade_doc)
    trade_id = str(inserted.inserted_id)
    inserted_ids = [inserted.inserted_id]

    try:
        # (a) accepted_unresolved
        payload_a = {
            "bridge_token": acc["bridge_token"],
            "trade_id": trade_id,
            "status": "pending",
            "error": "accepted_unresolved",
            "mt5_ticket": 0,
            "order_ticket": 111222333,
        }
        ra = requests.post(f"{BASE_URL}/api/bridge/report",
                           json=payload_a, timeout=15)
        assert ra.status_code == 200, ra.text
        body_a = ra.json()
        assert body_a.get("ok") is True
        assert body_a.get("unresolved") is True, body_a

        after_a = await db.trades.find_one({"_id": inserted.inserted_id})
        assert after_a["status"] == "pending"
        assert after_a["submission_state"] == "broker_accepted_unresolved"
        assert after_a.get("order_ticket") == 111222333
        assert "mt5_ticket" not in after_a or not after_a.get("mt5_ticket"), \
            f"mt5_ticket=0 must NOT be persisted; got {after_a.get('mt5_ticket')!r}"

        # (b) real fill with partial + position_volume
        payload_b = {
            "bridge_token": acc["bridge_token"],
            "trade_id": trade_id,
            "status": "open",
            "mt5_ticket": 999888777,       # position id
            "order_ticket": 111222333,
            "deal_ticket": 444555666,
            "position_id": 999888777,
            "filled_volume": 0.30,
            "position_volume": 1.50,       # broker netted symbol exposure
        }
        rb = requests.post(f"{BASE_URL}/api/bridge/report",
                           json=payload_b, timeout=15)
        assert rb.status_code == 200, rb.text
        assert rb.json().get("ok") is True

        after_b = await db.trades.find_one({"_id": inserted.inserted_id})
        assert after_b["status"] == "open"
        assert after_b.get("lot_size") == 0.30, after_b.get("lot_size")
        assert after_b.get("original_lot_size") == 1.00
        assert after_b.get("partial_fill") is True
        assert after_b.get("position_volume") == 1.50
        assert after_b.get("mt5_ticket") == 999888777
        assert after_b.get("order_ticket") == 111222333
        assert after_b.get("deal_ticket") == 444555666
        assert after_b.get("position_id") == 999888777

        pf_event = await db.trade_events.find_one(
            {"trade_id": trade_id, "event_type": "PartialFillAdopted"})
        assert pf_event is not None, "PartialFillAdopted event not written"

        # (c) replay accepted_unresolved AFTER open — must be ignored
        rc = requests.post(f"{BASE_URL}/api/bridge/report",
                           json=payload_a, timeout=15)
        assert rc.status_code == 200, rc.text
        body_c = rc.json()
        assert body_c.get("ok") is True
        assert body_c.get("ignored") == "already_open", body_c

        after_c = await db.trades.find_one({"_id": inserted.inserted_id})
        assert after_c["status"] == "open", after_c["status"]
        assert after_c.get("lot_size") == 0.30

    finally:
        # ALWAYS cleanup
        await db.trades.delete_many({"_id": {"$in": inserted_ids}})
        await db.trade_events.delete_many({"trade_id": trade_id})


@pytest.mark.asyncio
async def test_execution_health_has_unresolved_submissions(admin_cookies):
    r = requests.get(f"{BASE_URL}/api/bot/execution-health",
                     cookies=admin_cookies, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "unresolved_submissions" in body, list(body.keys())
    assert isinstance(body["unresolved_submissions"], int)
