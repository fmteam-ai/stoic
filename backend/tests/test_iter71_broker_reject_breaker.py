"""Tests for iter-71 — Broker-rejection circuit breaker.

Verifies the auto-halt logic that stops the bleed when a broker keeps
rejecting our orders. Specifically covers the VT Markets / suffixed-symbol
case where every SELL XAUUSD returns retcode=10013.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

from broker_reject_breaker import (
    evaluate_account, unblock_account, _extract_retcode, _RETCODE_HINTS,
    TRIP_AFTER, WINDOW_MINUTES,
)


def _get_db_url():
    mongo_url = "mongodb://localhost:27017"
    db_name = "test_database"
    try:
        with open("/app/backend/.env") as f:
            for line in f:
                if line.startswith("MONGO_URL="):
                    mongo_url = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("DB_NAME="):
                    db_name = line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return mongo_url, db_name


async def _with_db(coro_factory):
    mongo_url, db_name = _get_db_url()
    client = AsyncIOMotorClient(mongo_url)
    try:
        return await coro_factory(client[db_name])
    finally:
        client.close()


# ─────────── retcode extraction ───────────
def test_extract_retcode_basic():
    assert _extract_retcode("retcode=10013") == "10013"
    assert _extract_retcode("retcode=10016 INVALID_STOPS") == "10016"


def test_extract_retcode_none_on_empty():
    assert _extract_retcode(None) is None
    assert _extract_retcode("") is None
    assert _extract_retcode("no_retcode_here") is None


def test_extract_retcode_v129_symbol_not_found_tag():
    assert _extract_retcode("symbol_not_found:XAUUSD") == "SYMBOL_NOT_FOUND"


def test_hints_table_contains_critical_codes():
    """The known-cause hints must cover all the retcodes we surface in the UI."""
    for code in ("10013", "10014", "10016", "10018", "10019", "10027",
                 "SYMBOL_NOT_FOUND"):
        assert code in _RETCODE_HINTS
        label, hint = _RETCODE_HINTS[code]
        assert label and hint, f"missing label/hint for {code}"


# ─────────── evaluate_account ───────────
def test_evaluate_no_trip_with_no_failures():
    """Empty failure history → not blocked."""
    acct_id = ObjectId()

    async def _go(db):
        await db.accounts.insert_one({
            "_id": acct_id, "user_id": "test_user", "label": "iter71_clean",
            "bridge_token": f"iter71_{acct_id}", "mode": "live",
            "status": "connected",
        })
        try:
            acct = await db.accounts.find_one({"_id": acct_id})
            return await evaluate_account(db, acct)
        finally:
            await db.accounts.delete_one({"_id": acct_id})

    result = asyncio.run(_with_db(_go))
    assert result["blocked"] is False
    assert result["tripped_this_call"] is False
    assert result["consecutive_failures"] == 0


def test_evaluate_no_trip_below_threshold():
    """Only 2 failures (< TRIP_AFTER=3) → not blocked."""
    acct_id = ObjectId()
    user_id = f"test_user_{acct_id}"
    trade_ids = [ObjectId() for _ in range(2)]

    async def _go(db):
        await db.accounts.insert_one({
            "_id": acct_id, "user_id": user_id, "label": "iter71_two_fails",
            "bridge_token": f"iter71_two_{acct_id}",
            "mode": "live", "status": "connected",
        })
        now = datetime.now(timezone.utc)
        await db.trades.insert_many([
            {"_id": tid, "user_id": user_id, "account_id": str(acct_id),
             "status": "failed", "error": "retcode=10013",
             "opened_at": (now - timedelta(minutes=i + 1)).isoformat()}
            for i, tid in enumerate(trade_ids)
        ])
        try:
            acct = await db.accounts.find_one({"_id": acct_id})
            return await evaluate_account(db, acct)
        finally:
            await db.accounts.delete_one({"_id": acct_id})
            await db.trades.delete_many({"_id": {"$in": trade_ids}})

    result = asyncio.run(_with_db(_go))
    assert result["blocked"] is False
    assert result["consecutive_failures"] == 2


def test_evaluate_trips_on_three_same_retcode():
    """Three retcode=10013 failures → trip, flag set in DB."""
    acct_id = ObjectId()
    user_id = f"test_user_{acct_id}"
    trade_ids = [ObjectId() for _ in range(3)]

    async def _seed(db):
        await db.accounts.insert_one({
            "_id": acct_id, "user_id": user_id, "label": "iter71_vt_sim",
            "bridge_token": f"iter71_vt_{acct_id}",
            "mode": "live", "status": "connected",
        })
        now = datetime.now(timezone.utc)
        await db.trades.insert_many([
            {"_id": tid, "user_id": user_id, "account_id": str(acct_id),
             "status": "failed", "error": "retcode=10013",
             "opened_at": (now - timedelta(minutes=i + 1)).isoformat()}
            for i, tid in enumerate(trade_ids)
        ])

    async def _run(db):
        acct = await db.accounts.find_one({"_id": acct_id})
        verdict = await evaluate_account(db, acct)
        updated = await db.accounts.find_one({"_id": acct_id})
        return verdict, updated

    async def _cleanup(db):
        await db.accounts.delete_one({"_id": acct_id})
        await db.trades.delete_many({"_id": {"$in": trade_ids}})

    asyncio.run(_with_db(_seed))
    try:
        verdict, updated = asyncio.run(_with_db(_run))
        assert verdict["blocked"] is True
        assert verdict["tripped_this_call"] is True
        assert verdict["retcode"] == "10013"
        assert verdict["label"] == "INVALID_REQUEST"
        assert "VT Markets" in verdict["hint"] or "suffix" in verdict["hint"]
        # DB state mirrors verdict
        assert updated["trading_blocked"] is True
        assert updated["block_retcode"] == "10013"
        assert updated["block_retcode_label"] == "INVALID_REQUEST"
        assert "blocked_at" in updated
    finally:
        asyncio.run(_with_db(_cleanup))


def test_evaluate_no_trip_on_mixed_retcodes():
    """3 failures with DIFFERENT retcodes → not a clean pattern, no trip."""
    acct_id = ObjectId()
    user_id = f"test_user_{acct_id}"
    trade_ids = [ObjectId() for _ in range(3)]
    errors = ["retcode=10013", "retcode=10016", "retcode=10013"]

    async def _seed(db):
        await db.accounts.insert_one({
            "_id": acct_id, "user_id": user_id, "label": "iter71_mixed",
            "bridge_token": f"iter71_mixed_{acct_id}",
            "mode": "live", "status": "connected",
        })
        now = datetime.now(timezone.utc)
        await db.trades.insert_many([
            {"_id": tid, "user_id": user_id, "account_id": str(acct_id),
             "status": "failed", "error": errors[i],
             "opened_at": (now - timedelta(minutes=i + 1)).isoformat()}
            for i, tid in enumerate(trade_ids)
        ])

    async def _run(db):
        acct = await db.accounts.find_one({"_id": acct_id})
        return await evaluate_account(db, acct)

    async def _cleanup(db):
        await db.accounts.delete_one({"_id": acct_id})
        await db.trades.delete_many({"_id": {"$in": trade_ids}})

    asyncio.run(_with_db(_seed))
    try:
        verdict = asyncio.run(_with_db(_run))
        assert verdict["blocked"] is False
    finally:
        asyncio.run(_with_db(_cleanup))


def test_already_blocked_short_circuits():
    """Pre-blocked account returns current state without re-checking trades."""
    acct_id = ObjectId()

    async def _go(db):
        await db.accounts.insert_one({
            "_id": acct_id, "user_id": "u_x",
            "label": "iter71_already_blocked",
            "bridge_token": f"iter71_pre_{acct_id}",
            "mode": "live", "status": "connected",
            "trading_blocked": True,
            "block_retcode": "10013",
            "block_retcode_label": "INVALID_REQUEST",
            "block_hint": "Test hint",
            "block_reason": "Pre-existing block",
            "block_failure_count": 5,
        })
        try:
            acct = await db.accounts.find_one({"_id": acct_id})
            return await evaluate_account(db, acct)
        finally:
            await db.accounts.delete_one({"_id": acct_id})

    verdict = asyncio.run(_with_db(_go))
    assert verdict["blocked"] is True
    assert verdict["tripped_this_call"] is False
    assert verdict["retcode"] == "10013"
    assert verdict["consecutive_failures"] == 5


def test_unblock_clears_flag():
    """unblock_account() removes the block + block_* metadata."""
    acct_id = ObjectId()

    async def _seed(db):
        await db.accounts.insert_one({
            "_id": acct_id, "user_id": "u_y",
            "label": "iter71_unblock",
            "bridge_token": f"iter71_unb_{acct_id}",
            "mode": "live", "status": "connected",
            "trading_blocked": True,
            "block_reason": "test", "block_retcode": "10013",
        })

    async def _run(db):
        ok = await unblock_account(db, acct_id)
        updated = await db.accounts.find_one({"_id": acct_id})
        return ok, updated

    async def _cleanup(db):
        await db.accounts.delete_one({"_id": acct_id})

    asyncio.run(_with_db(_seed))
    try:
        ok, updated = asyncio.run(_with_db(_run))
        assert ok is True
        assert updated["trading_blocked"] is False
        assert "block_reason" not in updated
        assert "block_retcode" not in updated
        assert "unblocked_at" in updated
    finally:
        asyncio.run(_with_db(_cleanup))


def test_old_failures_outside_window_dont_count():
    """Failures > WINDOW_MINUTES old → ignored."""
    acct_id = ObjectId()
    user_id = f"test_user_{acct_id}"
    trade_ids = [ObjectId() for _ in range(3)]

    async def _seed(db):
        await db.accounts.insert_one({
            "_id": acct_id, "user_id": user_id, "label": "iter71_old",
            "bridge_token": f"iter71_old_{acct_id}",
            "mode": "live", "status": "connected",
        })
        # All 3 failures are 1 hour old → outside 30min window
        now = datetime.now(timezone.utc)
        await db.trades.insert_many([
            {"_id": tid, "user_id": user_id, "account_id": str(acct_id),
             "status": "failed", "error": "retcode=10013",
             "opened_at": (now - timedelta(hours=1, minutes=i)).isoformat()}
            for i, tid in enumerate(trade_ids)
        ])

    async def _run(db):
        acct = await db.accounts.find_one({"_id": acct_id})
        return await evaluate_account(db, acct)

    async def _cleanup(db):
        await db.accounts.delete_one({"_id": acct_id})
        await db.trades.delete_many({"_id": {"$in": trade_ids}})

    asyncio.run(_with_db(_seed))
    try:
        verdict = asyncio.run(_with_db(_run))
        assert verdict["blocked"] is False
        assert verdict["consecutive_failures"] == 0
    finally:
        asyncio.run(_with_db(_cleanup))


def test_constants_sane():
    """Sanity checks on tunables."""
    assert TRIP_AFTER >= 2
    assert WINDOW_MINUTES > 0
