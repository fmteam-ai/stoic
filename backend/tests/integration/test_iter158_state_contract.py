"""iter-158 — review Phase A: canonical state contract + fail-closed truth.

1. account_truth(): UNKNOWN/STALE never collapse to zero; CONFLICTED when a
   fresh broker count disagrees with the local projection.
2. effective_state(): server-derived ladder PANIC > OFF > DISCONNECTED >
   BLOCKED > OBSERVING > ACTIVE.
3. contract()/quick_truth(): one read → consistent counts + worst-of truth;
   PANIC availability whenever exposure exists or cannot be ruled out.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_state_{uuid.uuid4().hex[:8]}"


def _iso(seconds_ago=0):
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds_ago)).isoformat()


def test_account_truth_never_fabricates_zero():
    from state_contract import account_truth
    # never heartbeated → UNKNOWN, broker count None (NOT 0)
    t = account_truth({"mode": "live", "open_positions": 3}, open_local=0)
    assert t["position_truth"] == "UNKNOWN"
    assert t["open_positions_broker"] is None
    assert t["execution_authority"] == "NONE"
    # stale heartbeat → STALE, stale broker count discarded
    t = account_truth({"mode": "live", "last_heartbeat": _iso(600),
                       "open_positions": 2}, open_local=0)
    assert t["position_truth"] == "STALE"
    assert t["open_positions_broker"] is None
    # fresh + agreeing → FRESH with FULL authority when identity verified
    t = account_truth({"mode": "live", "last_heartbeat": _iso(5),
                       "open_positions": 1,
                       "verified_identity": {"broker_server": "X"}},
                      open_local=1)
    assert t["position_truth"] == "FRESH"
    assert t["open_positions_broker"] == 1
    assert t["execution_authority"] == "FULL"
    # fresh but DISAGREEING → CONFLICTED
    t = account_truth({"mode": "live", "last_heartbeat": _iso(5),
                       "open_positions": 1}, open_local=0)
    assert t["position_truth"] == "CONFLICTED"
    # fresh but identity unverified → REDUCED authority
    assert t["execution_authority"] == "REDUCED"


def test_effective_state_ladder():
    from state_contract import effective_state
    fresh = {"ea_connected": True, "position_truth": "FRESH",
             "execution_authority": "FULL"}
    assert effective_state(bot_enabled=True, tripped=True, truth=fresh,
                           operational_mode="autonomous_live") == "PANIC"
    assert effective_state(bot_enabled=False, tripped=False, truth=fresh,
                           operational_mode="observe") == "OFF"
    assert effective_state(bot_enabled=True, tripped=False,
                           truth={**fresh, "ea_connected": False},
                           operational_mode="autonomous_live") == "DISCONNECTED"
    assert effective_state(bot_enabled=True, tripped=False,
                           truth={**fresh, "position_truth": "STALE"},
                           operational_mode="autonomous_live") == "BLOCKED"
    assert effective_state(bot_enabled=True, tripped=False,
                           truth={**fresh, "execution_authority": "REDUCED"},
                           operational_mode="autonomous_live") == "BLOCKED"
    assert effective_state(bot_enabled=True, tripped=False, truth=fresh,
                           operational_mode="observe") == "OBSERVING"
    assert effective_state(bot_enabled=True, tripped=False, truth=fresh,
                           operational_mode="autonomous_live") == "ACTIVE"


async def _contract_scenario():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    from state_contract import contract, quick_truth
    db = get_db()
    uid = "state_user"
    try:
        # 2 accounts: one fresh+verified with bot ON, one never connected
        a1 = await db.accounts.insert_one(
            {"user_id": uid, "label": "Live A", "mode": "live",
             "last_heartbeat": _iso(5), "open_positions": 1,
             "verified_identity": {"broker_server": "X"}})
        await db.accounts.insert_one(
            {"user_id": uid, "label": "Dead B", "mode": "live",
             "trading_enabled": False})
        await db.trades.insert_one(
            {"user_id": uid, "account_id": str(a1.inserted_id),
             "status": "open", "symbol": "XAUUSD"})
        await db.bot_configs.insert_one(
            {"user_id": uid, "account_id": str(a1.inserted_id),
             "active": True, "operational_mode": "autonomous_live"})

        c = await contract(db, uid)
        assert c["totals"] == {"accounts_total": 2, "accounts_enabled": 1,
                               "bots_enabled": 1, "eas_connected": 1,
                               "open_local": 1, "open_broker": None}
        assert c["position_truth"] == "UNKNOWN"      # worst-of (Dead B)
        assert c["as_of"]
        by = {r["label"]: r for r in c["accounts"]}
        assert by["Live A"]["effective_state"] == "ACTIVE"
        assert by["Live A"]["force_trade_allowed"] is True
        assert by["Dead B"]["effective_state"] == "OFF"
        assert by["Dead B"]["force_trade_allowed"] is False
        assert by["Dead B"]["open_positions_broker"] is None

        # quick truth: PANIC available (open>0 AND truth not fresh)
        q = await quick_truth(db, uid, open_local=1)
        assert q["panic_available"] is True
        assert q["open_trades_broker"] is None       # unknown ≠ zero
        assert q["effective_state"] == "ACTIVE"

        # flat + all accounts fresh → PANIC correctly unavailable
        await db.accounts.delete_many({"label": "Dead B"})
        await db.trades.delete_many({})
        await db.accounts.update_one(
            {"_id": a1.inserted_id}, {"$set": {"open_positions": 0}})
        q = await quick_truth(db, uid, open_local=0)
        assert q["position_truth"] == "FRESH"
        assert q["open_trades_broker"] == 0
        assert q["panic_available"] is False
    finally:
        await db.client.drop_database(DB_NAME)


def test_contract_and_quick_truth():
    asyncio.run(_contract_scenario())
