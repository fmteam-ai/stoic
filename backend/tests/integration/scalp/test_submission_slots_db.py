"""Round 17 — real-MongoDB integration tests for the submission-slot
semaphore (item 12): atomic conditional updates, unique index, concurrent
acquisition, renewal, crash/expiry recovery and the lifecycle sweep, all
against the actual database driver semantics (no fakes)."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND))


def _db():
    from dotenv import load_dotenv
    load_dotenv(BACKEND / ".env")
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
    except ImportError:
        pytest.skip("motor not installed")
    url, name = os.environ.get("MONGO_URL"), os.environ.get("DB_NAME")
    if not url or not name:
        pytest.skip("MONGO_URL/DB_NAME not configured")
    return AsyncIOMotorClient(url)[name]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def broker():
    """Unique broker name per test — isolates slot docs; cleaned up after."""
    name = f"r17it-{uuid.uuid4().hex[:8]}"
    yield name
    async def _cleanup():
        db = _db()
        await db.scalp_submission_slots.delete_many(
            {"broker_key": {"$regex": name}})
        await db.trades.delete_many({"broker": name,
                                     "scope": "scalp_fast_it_test"})
    _run(_cleanup())


class TestSlotsRealMongo:
    def test_unique_index_exists_and_enforced(self, broker):
        async def run():
            from scalp import engine as eng
            db = _db()
            info = await db.scalp_submission_slots.index_information()
            assert any("broker_key" in str(k) and "slot_id" in str(v.get("key"))
                       for k, v in info.items()), info
            bk = eng._broker_cap_key(broker)
            await eng._ensure_submission_slots(db, bk)
            import pymongo
            with pytest.raises(pymongo.errors.DuplicateKeyError):
                await db.scalp_submission_slots.insert_one(
                    {"broker_key": bk, "slot_id": 0})
        _run(run())

    def test_concurrent_acquire_never_oversubscribes(self, broker):
        async def run():
            from scalp import engine as eng
            db = _db()
            slots = await asyncio.gather(*[
                eng.acquire_broker_submission_slot(db, broker, f"acc{i}")
                for i in range(20)])
            granted = [s for s in slots if s]
            assert len(granted) == eng.MAX_BROKER_CONCURRENT_SUBMISSIONS
            assert len({s["slot_id"] for s in granted}) == len(granted)
            for s in granted:
                await eng.release_broker_submission_slot(db, s)
            live = await db.scalp_submission_slots.count_documents(
                {"broker_key": eng._broker_cap_key(broker),
                 "token": {"$ne": None}})
            assert live == 0
        _run(run())

    def test_token_fencing_on_real_conditional_updates(self, broker):
        async def run():
            from scalp import engine as eng
            db = _db()
            slot = await eng.acquire_broker_submission_slot(db, broker, "a1")
            assert slot
            forged = {**slot, "token": "not-the-token"}
            await eng.release_broker_submission_slot(db, forged)
            doc = await db.scalp_submission_slots.find_one(
                {"broker_key": slot["broker_key"],
                 "slot_id": slot["slot_id"]})
            assert doc["token"] == slot["token"]        # foreign release no-op
            assert await eng.renew_submission_slot(db, forged) is False
            assert await eng.renew_submission_slot(db, slot) is True
            await eng.release_broker_submission_slot(db, slot)
        _run(run())

    def test_expired_lease_reclaimed_by_new_worker(self, broker):
        async def run():
            from scalp import engine as eng
            db = _db()
            held = [await eng.acquire_broker_submission_slot(db, broker, f"a{i}")
                    for i in range(eng.MAX_BROKER_CONCURRENT_SUBMISSIONS)]
            assert all(held)
            assert await eng.acquire_broker_submission_slot(
                db, broker, "a9") is None
            # simulate a crashed holder: force one lease into the past
            past = (datetime.now(timezone.utc)
                    - timedelta(seconds=5)).isoformat()
            await db.scalp_submission_slots.update_one(
                {"broker_key": held[0]["broker_key"],
                 "slot_id": held[0]["slot_id"]},
                {"$set": {"lease_until": past}})
            re = await eng.acquire_broker_submission_slot(db, broker, "a9")
            assert re is not None and re["slot_id"] == held[0]["slot_id"]
            # crashed holder's old token can no longer renew/release it
            assert await eng.renew_submission_slot(db, held[0]) is False
            for s in held[1:] + [re]:
                await eng.release_broker_submission_slot(db, s)
        _run(run())

    def test_sweep_renews_pending_and_releases_terminal(self, broker):
        async def run():
            from scalp import engine as eng
            db = _db()
            s_pend = await eng.acquire_broker_submission_slot(db, broker, "aP")
            s_term = await eng.acquire_broker_submission_slot(db, broker, "aT")
            assert s_pend and s_term
            await db.trades.insert_many([
                {"scope": "scalp_fast", "status": "pending",
                 "mt5_ticket": None, "broker": broker,
                 "account_id": "aP", "submission_slot": dict(s_pend)},
                {"scope": "scalp_fast", "status": "open",
                 "mt5_ticket": 123456, "broker": broker,
                 "account_id": "aT", "submission_slot": dict(s_term)}])
            # mark both docs so the broker fixture cleanup finds them
            await db.trades.update_many(
                {"broker": broker}, {"$set": {"scope": "scalp_fast"}})
            before = (await db.scalp_submission_slots.find_one(
                {"broker_key": s_pend["broker_key"],
                 "slot_id": s_pend["slot_id"]}))["lease_until"]
            report = await eng.sweep_submission_slots(db)
            for _ in range(8):
                await asyncio.sleep(0)
            after_p = await db.scalp_submission_slots.find_one(
                {"broker_key": s_pend["broker_key"],
                 "slot_id": s_pend["slot_id"]})
            after_t = await db.scalp_submission_slots.find_one(
                {"broker_key": s_term["broker_key"],
                 "slot_id": s_term["slot_id"]})
            assert after_p["token"] == s_pend["token"]      # kept + renewed
            assert after_p["lease_until"] >= before
            assert after_t["token"] is None                 # terminal → freed
            assert report["released_terminal"] >= 1
            await db.trades.delete_many({"broker": broker})
            await eng.release_broker_submission_slot(db, s_pend)
        _run(run())

    def test_sweep_flags_pending_order_that_lost_its_slot(self, broker):
        async def run():
            from scalp import engine as eng
            db = _db()
            slot = await eng.acquire_broker_submission_slot(db, broker, "aV")
            assert slot
            await db.trades.insert_one(
                {"scope": "scalp_fast", "status": "pending",
                 "mt5_ticket": None, "broker": broker, "account_id": "aV",
                 "submission_slot": dict(slot)})
            # another holder takes the slot over (token changes)
            await db.scalp_submission_slots.update_one(
                {"broker_key": slot["broker_key"],
                 "slot_id": slot["slot_id"]},
                {"$set": {"token": "someone-else",
                          "lease_until": (datetime.now(timezone.utc)
                                          + timedelta(seconds=60)).isoformat()}})
            await eng.sweep_submission_slots(db)
            reason = eng.capacity_integrity_reason(slot["broker_key"])
            assert reason and "pending_order_lost_slot" in reason
            # cleanup: remove the trade; next sweep clears the violation
            await db.trades.delete_many({"broker": broker})
            await db.scalp_submission_slots.update_one(
                {"broker_key": slot["broker_key"],
                 "slot_id": slot["slot_id"]},
                {"$set": {"token": None, "lease_until": eng._EPOCH_ISO}})
            await eng.sweep_submission_slots(db)
            for _ in range(8):
                await asyncio.sleep(0)
            assert eng.capacity_integrity_reason(slot["broker_key"]) is None
        _run(run())

    def test_emergency_pool_is_separate_and_bounded(self, broker):
        async def run():
            from scalp import engine as eng
            db = _db()
            entry = await eng.acquire_broker_submission_slot(db, broker, "aE")
            em = await eng.acquire_broker_submission_slot(
                db, broker, "aE", pool="emergency")
            assert entry and em
            assert em["broker_key"].startswith("emergency:")
            n = await db.scalp_submission_slots.count_documents(
                {"broker_key": em["broker_key"]})
            assert n == eng.EMERGENCY_MAX_CONCURRENT
            await eng.release_broker_submission_slot(db, entry)
            await eng.release_broker_submission_slot(db, em)
        _run(run())
