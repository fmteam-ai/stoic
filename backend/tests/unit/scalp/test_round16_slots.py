"""Round 16 item 12 — leased-slot semaphore concurrency tests.

FakeSlots emulates MongoDB's per-document atomic conditional update_one:
each call yields to the event loop FIRST (forcing worker interleaving),
then performs the read-match-mutate synchronously (atomic, like Mongo).
A crashed holder is simulated by simply never releasing — its lease
expires and the slot becomes reclaimable, with token fencing preventing
any foreign renew/release."""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scalp import engine as eng
from scalp.engine import (acquire_broker_submission_slot,
                          release_broker_submission_slot,
                          renew_submission_slot)


class FakeSlots:
    def __init__(self):
        self.docs = {}

    def _match(self, doc, filt):
        for k, v in filt.items():
            if isinstance(v, dict):
                if "$lt" in v and not (doc.get(k) is not None
                                       and doc.get(k) < v["$lt"]):
                    return False
                if "$gte" in v and not (doc.get(k) is not None
                                        and doc.get(k) >= v["$gte"]):
                    return False
            elif doc.get(k) != v:
                return False
        return True

    async def update_one(self, filt, update, upsert=False):
        await asyncio.sleep(0)              # force interleaving BEFORE the
        matched = modified = 0              # atomic read-match-mutate below
        for doc in self.docs.values():
            if self._match(doc, filt):
                matched = 1
                if "$set" in update:
                    doc.update(update["$set"])
                    modified = 1
                break
        if matched == 0 and upsert:
            key = (filt["broker_key"], filt["slot_id"])
            if key not in self.docs:
                self.docs[key] = {**filt, **update.get("$setOnInsert", {})}
        return SimpleNamespace(matched_count=matched, modified_count=modified)

    async def count_documents(self, filt):
        await asyncio.sleep(0)
        return sum(1 for d in self.docs.values() if self._match(d, filt))

    async def delete_many(self, filt):
        await asyncio.sleep(0)
        keys = [k for k, d in self.docs.items() if self._match(d, filt)]
        for k in keys:
            del self.docs[k]
        return SimpleNamespace(deleted_count=len(keys))


def _db():
    db = SimpleNamespace()
    db.scalp_submission_slots = FakeSlots()
    return db


def _fresh(broker):
    eng._slots_ready.discard(f"broker:{broker.strip().lower()}")


def _expire(db, broker_key, n=None):
    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    done = 0
    for d in db.scalp_submission_slots.docs.values():
        if d["broker_key"] == broker_key and d.get("token"):
            d["lease_until"] = past
            done += 1
            if n is not None and done >= n:
                break
    return done


class TestRound16LeasedSlots:
    def test_eight_workers_six_slots_exactly_six_granted(self):
        """8 concurrent workers contend for the 6-slot pool: exactly 6 win,
        each on a DISTINCT slot, 2 are denied — no oversubscription."""
        db = _db()
        _fresh("b16a")

        async def run():
            return await asyncio.gather(*[
                acquire_broker_submission_slot(db, "b16a", f"acc{i}")
                for i in range(8)])
        slots = asyncio.run(run())
        granted = [s for s in slots if s is not None]
        assert len(granted) == eng.MAX_BROKER_CONCURRENT_SUBMISSIONS == 6
        assert slots.count(None) == 2
        assert len({s["slot_id"] for s in granted}) == 6
        assert len({s["token"] for s in granted}) == 6

    def test_crashed_worker_lease_expires_and_slot_reclaimed(self):
        """A holder that dies without releasing frees capacity by lease
        EXPIRY alone; before expiry the pool stays fully committed."""
        db = _db()
        _fresh("b16b")

        async def run():
            held = [await acquire_broker_submission_slot(db, "b16b", f"a{i}")
                    for i in range(6)]
            assert all(held)
            # pool exhausted while all leases are live
            assert await acquire_broker_submission_slot(db, "b16b", "a9") is None
            # two workers "crash": their leases expire, nobody releases
            assert _expire(db, "broker:b16b", n=2) == 2
            re1 = await acquire_broker_submission_slot(db, "b16b", "a10")
            re2 = await acquire_broker_submission_slot(db, "b16b", "a11")
            re3 = await acquire_broker_submission_slot(db, "b16b", "a12")
            return re1, re2, re3
        re1, re2, re3 = asyncio.run(run())
        assert re1 is not None and re2 is not None
        assert re3 is None                     # only the 2 expired came back

    def test_release_and_renew_are_token_fenced(self):
        """Only the holder's token can release/renew its slot: a foreign
        (stale or forged) token is a strict no-op."""
        db = _db()
        _fresh("b16c")

        async def run():
            slot = await acquire_broker_submission_slot(db, "b16c", "a1")
            forged = {**slot, "token": "forged-token"}
            await release_broker_submission_slot(db, forged)
            doc = db.scalp_submission_slots.docs[(slot["broker_key"],
                                                  slot["slot_id"])]
            assert doc["token"] == slot["token"]      # still held
            assert await renew_submission_slot(db, forged) is False
            old_until = doc["lease_until"]
            assert await renew_submission_slot(db, slot) is True
            assert doc["lease_until"] >= old_until
            await release_broker_submission_slot(db, slot)
            assert doc["token"] is None               # truly released
            # released slot is immediately reacquirable
            again = await acquire_broker_submission_slot(db, "b16c", "a2")
            assert again is not None and again["slot_id"] == slot["slot_id"]
        asyncio.run(run())

    def test_per_account_fairness_cap(self):
        """One account can hold at most MAX_ACCOUNT_ACTIVE_SUBMISSIONS
        even while the broker pool has free slots."""
        db = _db()
        _fresh("b16d")

        async def run():
            got = [await acquire_broker_submission_slot(db, "b16d", "acc1")
                   for _ in range(eng.MAX_ACCOUNT_ACTIVE_SUBMISSIONS + 1)]
            assert all(g is not None
                       for g in got[:eng.MAX_ACCOUNT_ACTIVE_SUBMISSIONS])
            assert got[-1] is None                    # fairness denial
            # a DIFFERENT account still gets a slot from the free pool
            other = await acquire_broker_submission_slot(db, "b16d", "acc2")
            assert other is not None
        asyncio.run(run())

    def test_per_symbol_fairness_cap(self):
        """Round 16 item 11 — one symbol can never monopolize the pool:
        capped at MAX_SYMBOL_ACTIVE_SUBMISSIONS across accounts."""
        db = _db()
        _fresh("b16e")

        async def run():
            for i in range(eng.MAX_SYMBOL_ACTIVE_SUBMISSIONS):
                s = await acquire_broker_submission_slot(
                    db, "b16e", f"acc{i}", symbol="EURUSD")
                assert s is not None
            denied = await acquire_broker_submission_slot(
                db, "b16e", "acc99", symbol="EURUSD")
            assert denied is None
            # another symbol is unaffected
            gbp = await acquire_broker_submission_slot(
                db, "b16e", "acc99", symbol="GBPUSD")
            assert gbp is not None
        asyncio.run(run())

    def test_no_double_grant_under_sustained_churn(self):
        """20 workers × acquire/hold/release cycles: the live-token count
        never exceeds the pool size and a slot is never granted twice."""
        db = _db()
        _fresh("b16f")
        active: set = set()
        peak = {"v": 0}
        violations = []

        async def worker(i):
            for _ in range(5):
                slot = await acquire_broker_submission_slot(
                    db, "b16f", f"acc{i}")
                if slot is None:
                    await asyncio.sleep(0)
                    continue
                key = (slot["broker_key"], slot["slot_id"])
                if key in active:
                    violations.append(key)            # double-grant!
                active.add(key)
                peak["v"] = max(peak["v"], len(active))
                await asyncio.sleep(0)
                active.discard(key)
                await release_broker_submission_slot(db, slot)

        async def run():
            await asyncio.gather(*[worker(i) for i in range(20)])
        asyncio.run(run())
        assert violations == []
        assert 0 < peak["v"] <= eng.MAX_BROKER_CONCURRENT_SUBMISSIONS

    def test_slot_documents_carry_audit_fields(self):
        """Every held slot records who/what/when for operational audit."""
        db = _db()
        _fresh("b16g")

        async def run():
            return await acquire_broker_submission_slot(
                db, "b16g", "accA", "dec-1", symbol="EURUSD")
        slot = asyncio.run(run())
        doc = db.scalp_submission_slots.docs[(slot["broker_key"],
                                              slot["slot_id"])]
        assert doc["account_id"] == "accA"
        assert doc["decision_id"] == "dec-1"
        assert doc["symbol"] == "EURUSD"
        assert doc["worker_id"] == eng._worker_id
        assert doc["acquired_at"] and doc["lease_until"] > doc["acquired_at"]
