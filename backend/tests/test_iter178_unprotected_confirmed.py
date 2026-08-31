"""iter-178 — prod Bot Health 45 loop, part 2 (unprotected_positions).

The unprotected-positions detector must agree with the protection state
machine: confirmed_stop_loss is written exclusively from broker-confirmed
evidence, so trades carrying it are PROTECTED even when the legacy
stop_loss field was never populated (emergency-protection path). The old
query ignored confirmed_stop_loss and re-raised the critical alert forever.
"""
import asyncio
import datetime as dt
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
UID = "_t178_user"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _trade(**kw):
    base = {"user_id": UID, "status": "open", "symbol": "EURUSD",
            "opened_at": (dt.datetime.now(dt.timezone.utc)
                          - dt.timedelta(hours=1)).isoformat()}
    base.update(kw)
    return base


async def _scenario():
    from motor.motor_asyncio import AsyncIOMotorClient
    from protection_guard import unprotected_open_query

    db = AsyncIOMotorClient(MONGO_URL)[DB_NAME]
    try:
        await db.trades.insert_many([
            # emergency path resolved: no stop_loss but broker CONFIRMED
            _trade(stop_loss=None, confirmed_stop_loss=1.0850,
                   protection_state="RESOLVED", mt5_ticket=1001),
            # genuinely unprotected: no stop, no confirmation
            _trade(stop_loss=None, mt5_ticket=1002),
            # local SL proposed but broker never confirmed the (re)arm
            _trade(stop_loss=1.0900, confirmed_stop_loss=0,
                   lifecycle_state="PROTECTION_REQUESTED", mt5_ticket=1003),
            # fully verified via heartbeat snapshot: lifecycle OPEN
            _trade(stop_loss=1.0900, confirmed_stop_loss=1.0900,
                   lifecycle_state="OPEN", mt5_ticket=1004),
        ])
        q = unprotected_open_query({"user_id": UID})
        n = await db.trades.count_documents(q)
        tickets = sorted([t["mt5_ticket"] async for t in
                          db.trades.find(q, {"mt5_ticket": 1})])
        return n, tickets
    finally:
        await db.trades.delete_many({"user_id": UID})


def test_confirmed_protection_not_counted():
    n, tickets = _run(_scenario())
    assert n == 2, f"expected 2 unprotected, got {n} ({tickets})"
    assert tickets == [1002, 1003], tickets
