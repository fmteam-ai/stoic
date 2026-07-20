"""Iteration 56 testing-agent adversarial edge-case (Round 17 spec):

Insert a synthetic pending scalp trade with submission_slot.token that is
DIFFERENT from the real slot's owner, run sweep_submission_slots directly,
verify capacity_integrity_reason surfaces 'pending_order_lost_slot' for
that broker AND that a subsequent clean sweep (after we fix the mismatch
by deleting the synthetic trade) clears the reason back to None.

Always cleans up its own synthetic slot+trade rows (broker prefix r17ta-).
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(BACKEND))


def _db():
    from dotenv import load_dotenv
    load_dotenv(BACKEND / ".env")
    from motor.motor_asyncio import AsyncIOMotorClient
    url, name = os.environ.get("MONGO_URL"), os.environ.get("DB_NAME")
    if not url or not name:
        pytest.skip("MONGO_URL/DB_NAME not configured")
    return AsyncIOMotorClient(url)[name]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def broker():
    name = f"r17ta-{uuid.uuid4().hex[:8]}"
    yield name

    async def _cleanup():
        db = _db()
        await db.scalp_submission_slots.delete_many(
            {"broker_key": {"$regex": name}})
        await db.trades.delete_many({"broker": name})
    _run(_cleanup())


class TestIter56CapacityIntegrityAdversarial:
    def test_pending_order_lost_slot_flag_then_clears(self, broker):
        async def run():
            from bson import ObjectId
            from scalp import engine as eng
            db = _db()
            bk = eng._broker_cap_key(broker)
            await eng._ensure_submission_slots(db, bk)

            # 1. Winner legitimately acquires a slot.
            winner_handle = await eng.acquire_broker_submission_slot(
                db, broker, account_id="acct-A", symbol="EURUSD",
                pool="entry")
            assert winner_handle is not None, \
                "should have acquired an entry slot"

            # 2. Insert a synthetic pending scalp trade whose recorded
            #    submission_slot.token is a DIFFERENT (stale) token.
            stale_token = f"stale-{uuid.uuid4().hex[:12]}"
            synthetic = {
                "_id": ObjectId(),
                "broker": broker,
                "scope": "scalp_fast",
                "status": "pending",
                "symbol": "EURUSD",
                "submission_slot": {
                    "broker_key": winner_handle["broker_key"],
                    "slot_id": winner_handle["slot_id"],
                    "token": stale_token,
                },
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            await db.trades.insert_one(synthetic)

            # 3. Baseline: no violation before sweep sees the mismatch.
            assert eng.capacity_integrity_reason(bk) in (None, )

            # 4. Run sweep — must report pending_order_lost_slot for bk.
            report = await eng.sweep_submission_slots(db)
            violation_types = [v["type"] for v in report["violations"]
                               if v.get("broker_key") == bk]
            assert "pending_order_lost_slot" in violation_types, \
                f"expected pending_order_lost_slot in {report}"

            reason = eng.capacity_integrity_reason(bk)
            assert reason is not None and "pending_order_lost_slot" in reason,\
                f"expected reason set for {bk}, got {reason!r}"

            # 5. Fix the mismatch (delete synthetic trade) and re-sweep —
            #    reason must clear.
            await db.trades.delete_one({"_id": synthetic["_id"]})
            await eng.sweep_submission_slots(db)
            reason_after = eng.capacity_integrity_reason(bk)
            assert reason_after is None, \
                f"expected reason cleared after clean sweep, got {reason_after!r}"

            # 6. Release winner slot (housekeeping).
            await eng.release_broker_submission_slot(db, winner_handle)

        _run(run())
