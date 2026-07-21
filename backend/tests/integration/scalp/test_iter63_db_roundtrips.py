"""Iter-63 — DB round-trip tests moved OUT of tests/unit (review item 1):
unit tests must run without MongoDB; these need the real database."""
import os

import pytest
from dotenv import load_dotenv

load_dotenv("/app/backend/.env")

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_broker_stats_record_and_summary_roundtrip():
    from motor.motor_asyncio import AsyncIOMotorClient
    from scalp.broker_stats import record, summary
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = cli[os.environ["DB_NAME"]]
    broker = "TEST_iter63_broker"
    try:
        await record(db, broker, submissions=2, entry_slip_pips=0.2,
                     ack_ms=1200, spread_pips=0.4)
        await record(db, broker, rejects=1, entry_slip_pips=0.4)
        out = await summary(db, broker)
        assert out["totals"]["submissions"] == 2
        assert out["totals"]["rejects"] == 1
        assert out["totals"]["fills"] == 2
        assert out["totals"]["reject_rate"] == pytest.approx(1 / 3, abs=0.01)
        sess = list(out["sessions"].values())[0]
        assert sess["avg_entry_slippage_pips"] == pytest.approx(0.3, abs=0.01)
        assert sess["avg_fill_delay_ms"] == 1200
        assert sess["avg_spread_pips"] == 0.4
    finally:
        await db.scalp_broker_stats.delete_many({"broker_key": broker})
        cli.close()


@pytest.mark.asyncio
async def test_worker_leader_lease_excludes_second_holder():
    from motor.motor_asyncio import AsyncIOMotorClient
    import workers.base as wb
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = cli[os.environ["DB_NAME"]]
    name = "test_iter63_lease"
    try:
        await db.worker_leases.delete_one({"_id": name})
        assert await wb._try_acquire(db, name) is True
        original = wb.HOLDER
        wb.HOLDER = "other-holder"
        try:
            assert await wb._try_acquire(db, name) is False
        finally:
            wb.HOLDER = original
        assert await wb._try_acquire(db, name) is True
    finally:
        await db.worker_leases.delete_one({"_id": name})
        cli.close()


@pytest.mark.asyncio
async def test_risk_reservation_lifecycle_roundtrip():
    from motor.motor_asyncio import AsyncIOMotorClient
    from scalp import risk_reservations as rr
    cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = cli[os.environ["DB_NAME"]]
    acct = "TEST_iter63_acct"
    try:
        r = await rr.reserve(db, account_id=acct, user_id="u",
                             decision_id="d1", risk_usd=12.34, lot=0.02)
        assert await rr.unaccounted_count(db, acct) == 1   # no trade yet
        await rr.transition(db, r["reservation_id"], "QUEUED_UNCONFIRMED",
                            trade_id="t1")
        assert await rr.unaccounted_count(db, acct) == 0   # visible in trades
        await rr.transition(db, r["reservation_id"], "QUEUED_UNCONFIRMED",
                            trade_id="t1", uncertain=True)
        assert await rr.unaccounted_count(db, acct) == 1   # uncertain → held
        await rr.release_for_trade(db, "t1", "broker_ack")
        assert await rr.unaccounted_count(db, acct) == 0
        doc = await db.risk_reservations.find_one(
            {"reservation_id": r["reservation_id"]})
        assert doc["state"] == "RELEASED"
        assert doc["release_reason"] == "broker_ack"
        assert len(doc["transitions"]) >= 3
    finally:
        await db.risk_reservations.delete_many({"account_id": acct})
        cli.close()
