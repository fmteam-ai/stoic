"""Iter23 regression: trade reconciler.

Ensures that DB-open trades whose mt5_ticket is no longer reported by the EA
are auto-closed both via the reconcile_account helper (heartbeat path) and
via reconcile_user (manual button path including the open_positions==0
fallback for older EAs).
"""
import os
import sys
import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from bson import ObjectId
from pymongo import MongoClient

# Ensure /app/backend is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _now():
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def db():
    """Sync pymongo handle for direct seeding/inspection."""
    url = os.environ["MONGO_URL"]
    name = os.environ["DB_NAME"]
    client = MongoClient(url)
    yield client[name]
    client.close()


@pytest.fixture
def seeded(db):
    user_id = "iter23-recon-user"
    acc_doc = {
        "user_id": user_id,
        "label": "Iter23 Test Account",
        "broker": "test",
        "bridge_token": f"ITER23-{_now()}",
        "open_positions": 3,
        "last_heartbeat": _now(),
        "_test_iter23": True,
    }
    acc_id = db.accounts.insert_one(acc_doc).inserted_id
    tickets = [777001, 777002, 777003]
    trade_ids = []
    for i, tk in enumerate(tickets):
        r = db.trades.insert_one({
            "user_id": user_id,
            "account_id": str(acc_id),
            "symbol": "XAUUSD",
            "action": "BUY",
            "lot_size": 0.01,
            "entry_price": 4000 + i,
            "stop_loss": 3990 + i,
            "take_profit": 4020 + i,
            "status": "open",
            "mt5_ticket": tk,
            "_test_iter23": True,
            # older than the iter-90 45s reconcile grace window
            "opened_at": (datetime.now(timezone.utc)
                          - timedelta(seconds=120)).isoformat(),
        })
        trade_ids.append(str(r.inserted_id))

    yield {
        "user_id": user_id,
        "account_id": str(acc_id),
        "tickets": tickets,
        "trade_ids": trade_ids,
    }

    # Teardown
    db.trades.delete_many({"_test_iter23": True})
    db.accounts.delete_many({"_test_iter23": True})


def _run(coro):
    return _arun(coro)


# Round 9 — shared suite loop from conftest; no deprecated get_event_loop()
from conftest import run_async as _arun  # noqa: E402


def test_reconcile_closes_all_when_broker_reports_zero(db, seeded):
    from trade_reconciler import reconcile_account

    summary = _arun(reconcile_account(seeded["account_id"], [], source="test"))
    assert summary["closed_count"] == 3
    assert summary["candidates_in_db"] == 3

    closed = db.trades.count_documents({
        "account_id": seeded["account_id"], "status": "closed",
    })
    assert closed == 3
    sample = db.trades.find_one({"_id": ObjectId(seeded["trade_ids"][0])})
    assert sample["close_reason"].startswith("broker_reconciled_")
    assert sample.get("reconciled") is True


def test_reconcile_keeps_trades_still_open_at_broker(db, seeded):
    from trade_reconciler import reconcile_account

    summary = _arun(reconcile_account(seeded["account_id"], [777002], source="test"))
    assert summary["closed_count"] == 2

    still_open = db.trades.find_one({"mt5_ticket": 777002})
    assert still_open["status"] == "open"
    closed_count = db.trades.count_documents({
        "account_id": seeded["account_id"], "status": "closed",
    })
    assert closed_count == 2


def test_reconcile_user_skips_old_ea_with_nonzero_positions(db, seeded):
    """No open_tickets field + open_positions > 0 → must skip (avoid false closes)."""
    from trade_reconciler import reconcile_user

    result = _arun(reconcile_user(seeded["user_id"]))
    target = next(
        a for a in result["accounts"]
        if a.get("account_id") == seeded["account_id"]
    )
    assert target.get("skipped") is True
    assert result["total_closed"] == 0


def test_reconcile_user_count_fallback_when_zero(db, seeded):
    """No open_tickets field but heartbeat says open_positions=0 → safe close."""
    from trade_reconciler import reconcile_user

    db.accounts.update_one(
        {"_id": ObjectId(seeded["account_id"])},
        {"$set": {"open_positions": 0, "last_heartbeat": _now()}},
    )
    result = _arun(reconcile_user(seeded["user_id"]))
    assert result["total_closed"] == 3
    target = next(
        a for a in result["accounts"]
        if a.get("account_id") == seeded["account_id"]
    )
    assert target.get("fallback") == "open_positions==0"


def test_reconcile_skips_trades_without_mt5_ticket(db, seeded):
    """Pending/never-filled trades (no ticket) must NOT be touched."""
    from trade_reconciler import reconcile_account

    db.trades.update_one(
        {"_id": ObjectId(seeded["trade_ids"][0])},
        {"$set": {"mt5_ticket": None}},
    )
    summary = _arun(reconcile_account(seeded["account_id"], [], source="test"))
    assert summary["closed_count"] == 2
    untouched = db.trades.find_one({"_id": ObjectId(seeded["trade_ids"][0])})
    assert untouched["status"] == "open"


def test_reconcile_clears_pending_close_orphans(db, seeded):
    """REGRESSION (iter-23.1): user clicks × CLOSE → trade flips to
    status=pending with close_requested=True. EA never acks. Reconciler must
    close these too, not just status=open trades.
    """
    from trade_reconciler import reconcile_account

    db.trades.update_many(
        {"account_id": seeded["account_id"]},
        {"$set": {"status": "pending", "close_requested": True, "close_reason": "manual"}},
    )
    open_count = db.trades.count_documents({"account_id": seeded["account_id"], "status": "open"})
    assert open_count == 0

    summary = _arun(reconcile_account(seeded["account_id"], [], source="test"))
    assert summary["closed_count"] == 3, "Pending+close_requested orphans must be closed"
    closed_count = db.trades.count_documents({"account_id": seeded["account_id"], "status": "closed"})
    assert closed_count == 3


def test_reconcile_keeps_pending_open_without_close_request(db, seeded):
    """Pending-OPEN trades (waiting for EA to first execute, no close_requested)
    must NOT be touched — reconciler is for orphans, not never-filled new orders.
    """
    from trade_reconciler import reconcile_account

    db.trades.update_many(
        {"account_id": seeded["account_id"]},
        {"$set": {"status": "pending"}, "$unset": {"close_requested": ""}},
    )
    summary = _arun(reconcile_account(seeded["account_id"], [], source="test"))
    assert summary["closed_count"] == 0
    pending_still = db.trades.count_documents({"account_id": seeded["account_id"], "status": "pending"})
    assert pending_still == 3


def test_reconcile_user_closes_account_orphans(db, seeded):
    """REGRESSION (iter-23.2): trades attached to a deleted account become
    permanently orphaned (no broker to reconcile against). reconcile_user
    must sweep them with close_reason='account_deleted'.
    """
    from trade_reconciler import reconcile_user

    # Delete the account, leaving the 3 trades attached to a now-orphan account_id
    db.accounts.delete_one({"_id": ObjectId(seeded["account_id"])})

    result = _arun(reconcile_user(seeded["user_id"]))
    assert result["total_closed"] == 3
    # Sweep summary should show under "<deleted>" pseudo-account
    deleted_summary = next(
        s for s in result["accounts"] if s.get("account_id") == "<deleted>"
    )
    assert deleted_summary["closed_count"] == 3

    # All 3 trades flipped to closed with the right reason
    closed = db.trades.find({"_id": {"$in": [ObjectId(t) for t in seeded["trade_ids"]]}})
    for t in closed:
        assert t["status"] == "closed"
        assert t["close_reason"] == "account_deleted"
        assert t["reconciled"] is True


def test_reconcile_user_pending_close_via_count_fallback(db, seeded):
    """End-to-end: user clicks × CLOSE on all 3 → status=pending+close_req.
    Heartbeat says open_positions=0. reconcile_user must close all 3 via the
    count fallback, even though their status is pending (not open).
    """
    from trade_reconciler import reconcile_user

    db.trades.update_many(
        {"account_id": seeded["account_id"]},
        {"$set": {"status": "pending", "close_requested": True, "close_reason": "manual"}},
    )
    db.accounts.update_one(
        {"_id": ObjectId(seeded["account_id"])},
        {"$set": {"open_positions": 0, "last_heartbeat": _now()}},
    )
    result = _arun(reconcile_user(seeded["user_id"]))
    assert result["total_closed"] == 3


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
