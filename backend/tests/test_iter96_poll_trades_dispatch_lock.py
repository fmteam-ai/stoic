"""iter-96 · /bridge/poll-trades must NOT re-dispatch the same pending
trade to the EA on successive polls within the dispatch-lock window.

Before this fix, poll_trades returned every pending trade every poll. If
`/bridge/report` was slow (or the response was dropped), the EA would
re-execute the same pending trade on its next poll, opening a duplicate
broker position with a new ticket. The duplicate then came in via
`/bridge/external-deal` fresh-insert with SL=0/TP=0 because the pending
sibling had already been claimed for the original ticket.

Post-fix: each poll ATOMICALLY stamps `_dispatched_at` and only returns
trades whose `_dispatched_at` is NULL or older than 30s. Duplicates are
prevented at the source.
"""
from __future__ import annotations
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)

import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta

_BACKEND_DIR = _BACKEND_DIR
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
with open(f"{_BACKEND_DIR}/.env") as _f:
    for _ln in _f:
        if "=" in _ln and not _ln.lstrip().startswith("#"):
            _k, _v = _ln.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

from bson import ObjectId  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_account_and_trade(db, acc_oid, user_id, token, symbol="GOLD#", action="SELL"):
    async def _b():
        await db.accounts.insert_one({
            "_id": acc_oid,
            "user_id": user_id,
            "bridge_token": token,
            "broker": "OnEquity",
        })
        now = datetime.now(timezone.utc).isoformat()
        await db.trades.insert_one({
            "user_id": user_id,
            "account_id": str(acc_oid),
            "symbol": symbol,
            "action": action,
            "lot_size": 0.13,
            "entry_price": 4062.68,
            "stop_loss": 4079.50,
            "take_profit": 4058.50,
            "status": "pending",
            "mt5_ticket": None,
            "opened_at": now,
        })
    return _b()


def test_poll_trades_locks_dispatch_within_window():
    async def _body():
        from routes import bridge_routes as br
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        test_db_name = os.environ["DB_NAME"] + "_test_iter96"
        db = client[test_db_name]
        try:
            br.get_db = lambda: db  # type: ignore[assignment]

            acc_oid = ObjectId()
            user_id = str(uuid.uuid4()).replace("-", "")[:24]
            token = f"tok-{uuid.uuid4().hex[:8]}"
            await _make_account_and_trade(db, acc_oid, user_id, token)

            poll = br.PollRequest(bridge_token=token)

            # First poll — should return the trade
            r1 = await br.poll_trades(poll)
            assert len(r1["trades"]) == 1, r1
            trade_id = r1["trades"][0]["trade_id"]

            # Second poll <1s later — MUST NOT return the same trade (locked)
            r2 = await br.poll_trades(poll)
            assert r2["trades"] == [], (
                "Dispatch lock broken — same trade returned twice!"
            )

            # Verify the lock timestamp + dispatch count in DB
            doc = await db.trades.find_one({"_id": ObjectId(trade_id)})
            assert doc["_dispatched_at"], "dispatch_at missing"
            assert doc["_dispatch_count"] == 1, doc.get("_dispatch_count")
        finally:
            await client.drop_database(test_db_name)
            client.close()
    _run(_body())


def test_poll_trades_re_dispatches_after_lock_expires():
    """Safety: if EA never confirms /bridge/report, the trade gets re-dispatched
    once the lock expires (30s). We simulate expiry by manually backdating
    the _dispatched_at."""
    async def _body():
        from routes import bridge_routes as br
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        test_db_name = os.environ["DB_NAME"] + "_test_iter96b"
        db = client[test_db_name]
        try:
            br.get_db = lambda: db  # type: ignore[assignment]

            acc_oid = ObjectId()
            user_id = str(uuid.uuid4()).replace("-", "")[:24]
            token = f"tok-{uuid.uuid4().hex[:8]}"
            await _make_account_and_trade(db, acc_oid, user_id, token)

            poll = br.PollRequest(bridge_token=token)
            r1 = await br.poll_trades(poll)
            trade_id = r1["trades"][0]["trade_id"]

            # Backdate the dispatch stamp to 60s ago (past the 30s window)
            await db.trades.update_one({"_id": ObjectId(trade_id)}, {
                "$set": {"_dispatched_at":
                         (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()}
            })

            r2 = await br.poll_trades(poll)
            assert len(r2["trades"]) == 1, "Expired lock should re-dispatch"
            doc = await db.trades.find_one({"_id": ObjectId(trade_id)})
            assert doc["_dispatch_count"] == 2, doc.get("_dispatch_count")
        finally:
            await client.drop_database(test_db_name)
            client.close()
    _run(_body())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
