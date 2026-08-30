"""iter-160 — Post-implementation review P0 corrections.

P0-1: accounts_overview backfills trading_enabled from bot state and
      reports bots_enabled; counts derive from the persisted flag.
P0-2: effective_connection_state is THE single connection rule.
P0-3: bot health-score fail-closed hard caps (stale truth / blocked
      execution can never read EXCELLENT).
P0-4: /authority position-truth pill can never say FULL while the
      caller's enabled accounts carry stale positions.
P0-5: FORCE TRADE (test-trade) is refused without full per-account
      go-live certification (CERTIFIED_DEMO_TEST for demo accounts).
P0-6: covered by updated test_iter154 grade/issue tests.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_p0_{uuid.uuid4().hex[:8]}"


def _now_dt():
    return datetime.now(timezone.utc)


def _fresh_db():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    return get_db()


def test_effective_connection_state_rule():
    from state_contract import HEARTBEAT_FRESH_S, effective_connection_state
    now = _now_dt()
    fresh = {"last_heartbeat": (now - timedelta(seconds=30)).isoformat()}
    c = effective_connection_state(fresh, now)
    assert c["state"] == "CONNECTED" and c["connected"] is True
    assert c["threshold_seconds"] == HEARTBEAT_FRESH_S == 180

    stale = {"last_heartbeat": (now - timedelta(seconds=600)).isoformat()}
    c = effective_connection_state(stale, now)
    assert c["state"] == "STALE" and c["connected"] is False

    dead = {"last_heartbeat": (now - timedelta(hours=5)).isoformat()}
    assert effective_connection_state(dead, now)["state"] == "DISCONNECTED"

    assert effective_connection_state({}, now)["state"] == "NEVER_CONNECTED"

    paper = {"mode": "paper"}
    c = effective_connection_state(paper, now)
    assert c["state"] == "PAPER" and c["connected"] is True


async def _overview_scenario():
    db = _fresh_db()
    from routes.account_routes import accounts_overview
    uid = "p0_user"
    now = _now_dt()
    a_on = ObjectId()   # active bot, no trading_enabled flag → backfills True
    a_off = ObjectId()  # no bot, no flag → backfills False
    try:
        await db.accounts.insert_many([
            {"_id": a_on, "user_id": uid, "label": "on",
             "last_heartbeat": (now - timedelta(seconds=20)).isoformat()},
            {"_id": a_off, "user_id": uid, "label": "off",
             "last_heartbeat": (now - timedelta(hours=2)).isoformat()}])
        await db.bot_configs.insert_one(
            {"user_id": uid, "account_id": str(a_on), "active": True})
        out = await accounts_overview(user={"id": uid})
        tot = out["totals"]
        assert tot["accounts"] == 2
        assert tot["trading_enabled"] == 1      # derived from persisted flag
        assert tot["bots_enabled"] == 1
        assert tot["connected"] == 1            # single 180s rule
        # flag PERSISTED (never inferred from record presence again)
        on_doc = await db.accounts.find_one({"_id": a_on})
        off_doc = await db.accounts.find_one({"_id": a_off})
        assert on_doc["trading_enabled"] is True
        assert off_doc["trading_enabled"] is False
        rows = {r["label"]: r for r in out["accounts"]}
        assert rows["on"]["connection_state"]["state"] == "CONNECTED"
        assert rows["off"]["connection_state"]["state"] == "DISCONNECTED"
    finally:
        await db.client.drop_database(DB_NAME)


def test_accounts_overview_enabled_and_bots():
    asyncio.run(_overview_scenario())


async def _health_cap_scenario():
    db = _fresh_db()
    from routes.bot_routes import bot_health_score
    uid = "p0_health_user"
    now = _now_dt()
    acc = ObjectId()
    try:
        # connected + certified-looking account so base deductions are small,
        # but the bot-enabled account has STALE heartbeat → truth STALE
        await db.accounts.insert_one(
            {"_id": acc, "user_id": uid, "label": "hc", "status": "connected",
             "trading_enabled": True, "ea_version": "1.56",
             "last_heartbeat": (now - timedelta(seconds=800)).isoformat(),
             "equity": 10000})
        await db.bot_configs.insert_one(
            {"user_id": uid, "account_id": str(acc), "active": True})
        out = await bot_health_score(user={"id": uid, "role": "user"})
        caps = {c["code"]: c for c in out.get("hard_caps") or []}
        assert "position_truth_not_fresh" in caps
        assert out["score"] <= 25
        assert out["status"] == "critical"
        # a FRESH heartbeat lifts the truth cap
        await db.accounts.update_one(
            {"_id": acc},
            {"$set": {"last_heartbeat":
                      (_now_dt() - timedelta(seconds=10)).isoformat(),
                      "open_positions": 0}})
        out2 = await bot_health_score(user={"id": uid, "role": "user"})
        codes = {c["code"] for c in out2.get("hard_caps") or []}
        assert "position_truth_not_fresh" not in codes
    finally:
        await db.client.drop_database(DB_NAME)


def test_health_score_fail_closed_caps():
    asyncio.run(_health_cap_scenario())


async def _authority_scenario():
    db = _fresh_db()
    from routes.authority_routes import authority_ep
    uid = "p0_auth_user"
    now = _now_dt()
    try:
        await db.accounts.insert_one(
            {"_id": ObjectId(), "user_id": uid, "label": "st",
             "trading_enabled": True,
             "last_heartbeat": (now - timedelta(seconds=900)).isoformat()})
        out = await authority_ep(user={"id": uid, "role": "user"})
        pt = (out.get("domains") or {}).get("position_truth") or {}
        assert pt.get("level") in ("STALE", "UNKNOWN", "CONFLICTED")
        assert pt.get("level") != "FULL"
        assert pt.get("enforce_level") == "CLOSE_ONLY"
        assert out.get("restricted") is True
    finally:
        await db.client.drop_database(DB_NAME)


def test_authority_position_truth_never_full_while_stale():
    asyncio.run(_authority_scenario())


async def _force_trade_scenario():
    db = _fresh_db()
    from fastapi import HTTPException
    from routes.account_routes import fire_test_trade
    uid = "p0_ft_user"
    now = _now_dt()
    acc = ObjectId()
    try:
        # FRESH heartbeat (passes the old gate) but 0 certification checks
        await db.accounts.insert_one(
            {"_id": acc, "user_id": uid, "label": "ft", "mode": "live",
             "account_type": "demo", "trading_enabled": True,
             "last_heartbeat": (now - timedelta(seconds=10)).isoformat()})
        with pytest.raises(HTTPException) as ei:
            await fire_test_trade(str(acc), user={"id": uid, "role": "user"})
        assert ei.value.status_code == 409
        detail = ei.value.detail
        assert detail["code"] == "force_trade_not_certified"
        assert detail["gate"] == "CERTIFIED_DEMO_TEST"
        assert "heartbeat" not in detail["failing_checks"]  # hb is fresh
        assert "ea_version" in detail["failing_checks"]
    finally:
        await db.client.drop_database(DB_NAME)


def test_force_trade_requires_full_certification():
    asyncio.run(_force_trade_scenario())
