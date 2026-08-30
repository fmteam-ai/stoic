"""iter-172 — "BOT REQUESTED ON · EXECUTION BLOCKED" must explain WHY.

1. account_truth(): REDUCED/NONE authority carries authority_reason.
2. state_reason(): every non-executing effective_state has a human WHY.
3. quick_truth(): effective_reason surfaces the dominant blocked cause.
4. readiness(): EXECUTION_BLOCKED message/recovery/accounts carry the
   exact per-account blocking cause (no more vague "resolve the listed
   condition").
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_blkreason_{uuid.uuid4().hex[:8]}"


def _iso(seconds_ago=0):
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds_ago)).isoformat()


def test_authority_reason_present():
    from state_contract import account_truth
    # connected but unverified identity → REDUCED + pairing instruction
    t = account_truth({"mode": "live", "last_heartbeat": _iso(5),
                       "open_positions": 0}, open_local=0)
    assert t["execution_authority"] == "REDUCED"
    assert "identity not verified" in t["authority_reason"]
    # offline → NONE + reason
    t = account_truth({"mode": "live"}, open_local=0)
    assert t["execution_authority"] == "NONE"
    assert "not connected" in t["authority_reason"]
    # verified + fresh → FULL, no reason
    t = account_truth({"mode": "live", "last_heartbeat": _iso(5),
                       "open_positions": 0,
                       "verified_identity": {"broker_server": "X"}},
                      open_local=0)
    assert t["execution_authority"] == "FULL"
    assert t["authority_reason"] is None


def test_state_reason_ladder():
    from state_contract import account_truth, state_reason
    unverified = account_truth({"mode": "live", "last_heartbeat": _iso(5),
                                "open_positions": 0}, open_local=0)
    r = state_reason(eff="BLOCKED", truth=unverified,
                     operational_mode="live")
    assert "identity not verified" in r
    stale = account_truth({"mode": "live", "last_heartbeat": _iso(600),
                           "open_positions": 0}, open_local=0)
    r = state_reason(eff="BLOCKED", truth=stale, operational_mode="live")
    assert "position truth STALE" in r
    assert state_reason(eff="ACTIVE", truth=unverified,
                        operational_mode="live") is None
    assert "panic" in state_reason(eff="PANIC", truth=unverified,
                                   operational_mode="live")
    assert "signals only" in state_reason(eff="OBSERVING", truth=unverified,
                                          operational_mode="observe")


async def _scenario():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    from state_contract import quick_truth
    from trading_readiness import readiness
    db = get_db()
    uid = "blkreason_user"
    try:
        # fresh EA heartbeat, bot ON, but identity NOT verified → BLOCKED
        a = await db.accounts.insert_one(
            {"user_id": uid, "label": "Unpaired", "mode": "live",
             "last_heartbeat": _iso(5), "open_positions": 0})
        await db.bot_configs.insert_one(
            {"user_id": uid, "account_id": str(a.inserted_id),
             "active": True, "operational_mode": "autonomous_live"})

        q = await quick_truth(db, uid, open_local=0)
        assert q["effective_state"] == "BLOCKED"
        assert "identity not verified" in q["effective_reason"]

        rd = await readiness(db, uid)
        codes = {r["code"]: r for r in rd["reasons"]}
        exb = codes["EXECUTION_BLOCKED"]
        assert "identity not verified" in exb["message"]
        assert "identity not verified" in exb["recovery"]
        assert exb["accounts"][0]["label"] == "Unpaired"
        assert "identity not verified" in exb["accounts"][0]["reason"]

        # stale heartbeat → POSITION_TRUTH_STALE names each account with
        # its heartbeat age + the terminal-side recovery action
        await db.accounts.update_one(
            {"label": "Unpaired"},
            {"$set": {"last_heartbeat": _iso(2.7 * 3600)}})
        rd = await readiness(db, uid)
        codes = {r["code"]: r for r in rd["reasons"]}
        stale = codes["POSITION_TRUTH_STALE"]
        acc = stale["accounts"][0]
        assert acc["label"] == "Unpaired"
        assert "last EA heartbeat 2.7h ago" in acc["reason"]
        assert "MT5 terminal" in acc["reason"]
    finally:
        await db.client.drop_database(DB_NAME)


def test_readiness_and_quick_truth_carry_cause():
    asyncio.run(_scenario())
