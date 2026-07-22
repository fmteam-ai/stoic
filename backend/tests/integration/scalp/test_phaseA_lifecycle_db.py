"""Phase A — order state machine + outbox against the REAL database."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import uuid

import pytest
from dotenv import load_dotenv

load_dotenv(_os.path.join(_BACKEND_DIR, ".env"))

pytestmark = pytest.mark.integration


def _db():
    from motor.motor_asyncio import AsyncIOMotorClient
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    return cli, cli[os.environ["DB_NAME"]]


@pytest.mark.asyncio
async def test_full_lifecycle_roundtrip_and_idempotency():
    from scalp import order_state as osm
    cli, db = _db()
    ins = await db.trades.insert_one({"symbol": "TEST_PHASEA",
                                      "status": "pending"})
    tid = str(ins.inserted_id)
    try:
        path = [(osm.QUEUED, "q"), (osm.EA_CLAIMED, "c"),
                (osm.BROKER_ACCEPTED, "a"), (osm.OPEN, "o"),
                (osm.CLOSE_REQUESTED, "cr"), (osm.CLOSED, "cl"),
                (osm.FINANCIALLY_RECONCILED, "fr")]
        for st, k in path:
            assert await osm.apply(db, tid, st, f"{k}:{tid}") == "applied"
        # duplicate idem key → no-op
        assert await osm.apply(db, tid, osm.FINANCIALLY_RECONCILED,
                               f"fr:{tid}") == "duplicate"
        # invalid transition out of a terminal state → refused
        assert await osm.apply(db, tid, osm.OPEN, "late-open") == "invalid"
        doc = await db.trades.find_one({"_id": ins.inserted_id})
        assert doc["lifecycle_state"] == "FINANCIALLY_RECONCILED"
        assert [e["state"] for e in doc["lifecycle"]] == [s for s, _ in path]
        assert len(doc["lifecycle_keys"]) == len(path)
    finally:
        await db.trades.delete_one({"_id": ins.inserted_id})
        cli.close()


@pytest.mark.asyncio
async def test_outbox_enqueue_relay_exactly_once():
    from scalp import outbox
    from trade_events import build
    cli, db = _db()
    key = f"te:test:{uuid.uuid4().hex}"
    ev = build("BrokerSubmitted", user_id="test", trade_id="tX",
               account_id="test_phaseA", payload={"lot": 0.01})
    try:
        await outbox.enqueue(db, "trade_event", key, ev, publish_now=False)
        # duplicate enqueue keeps the FIRST payload (idempotent)
        await outbox.enqueue(db, "trade_event", key,
                             {**ev, "event_id": "SHOULD_NOT_WIN"},
                             publish_now=False)
        # relay twice — event must land exactly once
        await outbox.relay_once(db, only_key=key)
        await outbox.relay_once(db, only_key=key)
        n = await db.trade_events.count_documents({"event_id": ev["event_id"]})
        assert n == 1
        row = await db.outbox.find_one({"outbox_key": key})
        assert row["state"] == "published"
        assert row["payload"]["event_id"] == ev["event_id"]
    finally:
        await db.outbox.delete_many({"outbox_key": key})
        await db.trade_events.delete_many({"event_id": ev["event_id"]})
        cli.close()


@pytest.mark.asyncio
async def test_crash_recovery_pending_row_published_by_relay():
    """Simulates a crash after the awaited outbox insert but before the
    immediate publish: the relay alone must deliver the event."""
    from scalp import outbox
    from trade_events import build
    cli, db = _db()
    key = f"te:crash:{uuid.uuid4().hex}"
    ev = build("PositionClosed", user_id="test", trade_id="tY",
               account_id="test_phaseA", payload={"net_pnl_usd": 1.0})
    try:
        await outbox.enqueue(db, "trade_event", key, ev, publish_now=False)
        out = await outbox.relay_once(db)
        assert out["published"] >= 1
        assert await db.trade_events.count_documents(
            {"event_id": ev["event_id"]}) == 1
    finally:
        await db.outbox.delete_many({"outbox_key": key})
        await db.trade_events.delete_many({"event_id": ev["event_id"]})
        cli.close()
