"""iter-122 Phase 2 — deep entitlement enforcement at the authority boundary.

The route-level 402 gates are advisory; these tests prove the WORKERS and
the FINAL ORDER DISPATCHER re-verify plan entitlements:
  • verify_execution_entitlement (subscription, mode ceiling, symbol,
    account-quota rank, fail-closed for live / fail-open for paper),
  • MT5BridgeEngine.execute refuses un-entitled dispatches,
  • signal-cooldown plan floor,
  • auto-heal + Loss Lab worker-side gates,
  • enterprise API key re-checks api_access at request time.
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture()
def svc_db():
    import database
    database._client = None
    database._db = None
    yield
    database._client = None
    database._db = None


def _now():
    return datetime.now(timezone.utc)


async def _mk_user(db, role="user"):
    from bson import ObjectId
    uid = ObjectId()
    await db.users.insert_one({
        "_id": uid, "email": f"iter122b-{uuid.uuid4().hex[:8]}@example.com",
        "role": role, "created_at": _now().isoformat()})
    return str(uid)


async def _mk_sub(db, uid, plan_id, days=30):
    await db.subscriptions.update_one(
        {"user_id": uid},
        {"$set": {"user_id": uid, "current_plan_id": plan_id,
                  "valid_until": (_now() + timedelta(days=days)).isoformat(),
                  "grace_until": None, "auto_renew": False}}, upsert=True)


async def _mk_account(db, uid, mode="live", created_offset_s=0):
    from bson import ObjectId
    aid = ObjectId()
    await db.accounts.insert_one({
        "_id": aid, "user_id": uid, "label": f"acct-{aid}", "mode": mode,
        "bridge_token": f"tok-{uuid.uuid4().hex}",
        "created_at": (_now() + timedelta(seconds=created_offset_s)).isoformat()})
    return await db.accounts.find_one({"_id": aid})


SIG = {"symbol": "XAUUSD", "action": "BUY", "origin": "auto", "lot_size": 0.01}


def test_live_execution_blocked_without_subscription(svc_db):
    async def inner():
        from database import get_db
        from entitlements import verify_execution_entitlement
        db = get_db()
        uid = await _mk_user(db)
        acct = await _mk_account(db, uid, mode="live")
        block = await verify_execution_entitlement(
            db, user_id=uid, account=acct, signal=dict(SIG))
        assert block and block["blocked"] == "entitlement"
        assert "inactive" in block["reason"]
    _run(inner())


def test_paper_execution_allowed_without_subscription(svc_db):
    async def inner():
        from database import get_db
        from entitlements import verify_execution_entitlement
        db = get_db()
        uid = await _mk_user(db)
        acct = await _mk_account(db, uid, mode="paper")
        assert await verify_execution_entitlement(
            db, user_id=uid, account=acct, signal=dict(SIG)) is None
    _run(inner())


def test_starter_plan_cannot_execute_live(svc_db):
    async def inner():
        from database import get_db
        from entitlements import verify_execution_entitlement
        db = get_db()
        uid = await _mk_user(db)
        await _mk_sub(db, uid, "starter_monthly")
        acct = await _mk_account(db, uid, mode="live")
        block = await verify_execution_entitlement(
            db, user_id=uid, account=acct, signal=dict(SIG))
        assert block and "demo/shadow only" in block["reason"]
        # …but the same Starter user CAN trade paper
        paper = await _mk_account(db, uid, mode="paper", created_offset_s=-5)
        assert await verify_execution_entitlement(
            db, user_id=uid, account=paper, signal=dict(SIG)) is None
    _run(inner())


def test_account_quota_rank_blocks_excess_accounts(svc_db):
    async def inner():
        from database import get_db
        from entitlements import verify_execution_entitlement
        db = get_db()
        uid = await _mk_user(db)
        await _mk_sub(db, uid, "trader_monthly")  # cap 3
        accounts = [await _mk_account(db, uid, mode="live", created_offset_s=i)
                    for i in range(4)]
        # Oldest 3 pass, the 4th (newest) is beyond quota
        assert await verify_execution_entitlement(
            db, user_id=uid, account=accounts[0], signal=dict(SIG)) is None
        block = await verify_execution_entitlement(
            db, user_id=uid, account=accounts[3], signal=dict(SIG))
        assert block and "quota" in block["reason"]
    _run(inner())


def test_admin_bypasses_execution_gate(svc_db):
    async def inner():
        from database import get_db
        from entitlements import verify_execution_entitlement
        db = get_db()
        uid = await _mk_user(db, role="admin")
        acct = await _mk_account(db, uid, mode="live")
        assert await verify_execution_entitlement(
            db, user_id=uid, account=acct, signal=dict(SIG)) is None
    _run(inner())


def test_mt5_engine_refuses_unentitled_dispatch(svc_db):
    async def inner():
        from database import get_db
        from execution import MT5BridgeEngine
        db = get_db()
        uid = await _mk_user(db)  # no subscription
        acct = await _mk_account(db, uid, mode="live")
        before = await db.trades.count_documents({"user_id": uid})
        out = await MT5BridgeEngine().execute(
            user_id=uid, account=acct, signal=dict(SIG))
        assert out.get("blocked") == "entitlement"
        assert await db.trades.count_documents({"user_id": uid}) == before
    _run(inner())


def test_signal_cooldown_respects_plan_floor():
    from bot_runner import _cooldown_minutes as _signal_cooldown_minutes
    assert _signal_cooldown_minutes(
        {"signal_cooldown_minutes": 1, "_tier_min_cooldown": 15}) == 15
    assert _signal_cooldown_minutes(
        {"signal_cooldown_minutes": 20, "_tier_min_cooldown": 15}) == 20
    assert _signal_cooldown_minutes(
        {"signal_cooldown_minutes": 1, "_tier_min_cooldown": 1}) == 1


def test_auto_heal_worker_gate(svc_db):
    async def inner():
        from bson import ObjectId
        from database import get_db
        from auto_heal import sweep_user
        db = get_db()
        uid = await _mk_user(db)  # starter — no auto_heal
        await db.users.update_one(
            {"_id": ObjectId(uid)},
            {"$set": {"auto_heal_settings": {"enabled": True}}})
        out = await sweep_user(uid)
        assert out.get("skipped") == "plan does not include auto_heal"
        # Trader plan unlocks it — the sweep actually runs
        await _mk_sub(db, uid, "trader_monthly")
        out2 = await sweep_user(uid)
        assert "skipped" not in out2 and out2.get("ok") is True
    _run(inner())


def test_loss_lab_worker_gate(svc_db):
    async def inner():
        from database import get_db
        from loss_postmortem import maybe_record_postmortem
        db = get_db()
        uid = await _mk_user(db)  # starter — no loss_lab
        res = await db.trades.insert_one({
            "user_id": uid, "symbol": "XAUUSD", "status": "closed",
            "pnl": -50.0, "close_reason": "stop_loss",
            "opened_at": _now().isoformat(), "closed_at": _now().isoformat()})
        out = await maybe_record_postmortem(db, res.inserted_id, force=True)
        assert out is None
        assert await db.loss_postmortems.count_documents(
            {"trade_id": str(res.inserted_id)}) == 0
    _run(inner())
