"""iter-103 — safety-review corrections: migration policy, allocator
evidence floors, silent-failure accounting."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from operational_modes import (migrate_default_modes,
                               remigrate_autonomous_to_supervised)
from silent_failures import record_swallow, swallow_counters


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter103-{uuid.uuid4().hex[:8]}"


def test_migration_assigns_supervised_live_not_autonomous(db):
    async def go():
        await db.bot_configs.insert_many([
            {"user_id": UID, "account_id": "m1", "active": True},
            {"user_id": UID, "account_id": "m2", "active": False},
        ])
        await migrate_default_modes(db)
        active = await db.bot_configs.find_one(
            {"user_id": UID, "account_id": "m1"})
        inactive = await db.bot_configs.find_one(
            {"user_id": UID, "account_id": "m2"})
        assert active["operational_mode"] == "supervised_live"
        assert active["mode_migration_policy"] == \
            "legacy_active_to_supervised_live"
        assert inactive["operational_mode"] == "observe"
        await db.bot_configs.delete_many({"user_id": UID})
    _run(go())


def test_remigration_demotes_grandfathered_autonomous(db):
    async def go():
        uid = f"{UID}-rm"
        await db.platform_state.delete_one(
            {"_id": "mode_safety_remigration"})
        await db.bot_configs.insert_many([
            # grandfathered — no explicit promotion record → demote
            {"user_id": uid, "account_id": "g1", "active": True,
             "operational_mode": "autonomous_live"},
            # explicitly promoted (stamp) → keep
            {"user_id": uid, "account_id": "g2", "active": True,
             "operational_mode": "autonomous_live",
             "mode_explicitly_promoted": True},
        ])
        res = await remigrate_autonomous_to_supervised(db)
        assert res["demoted"] >= 1
        g1 = await db.bot_configs.find_one(
            {"user_id": uid, "account_id": "g1"})
        g2 = await db.bot_configs.find_one(
            {"user_id": uid, "account_id": "g2"})
        assert g1["operational_mode"] == "supervised_live"
        assert g2["operational_mode"] == "autonomous_live"
        # idempotent — second run is a no-op
        res2 = await remigrate_autonomous_to_supervised(db)
        assert res2["already_done"] is True
        await db.bot_configs.delete_many({"user_id": uid})
        await db.platform_state.delete_one(
            {"_id": "mode_safety_remigration"})
    _run(go())


def test_remigration_keeps_governance_promoted_configs(db):
    async def go():
        uid = f"{UID}-gv"
        await db.platform_state.delete_one(
            {"_id": "mode_safety_remigration"})
        await db.bot_configs.insert_one(
            {"user_id": uid, "account_id": "g3", "active": True,
             "operational_mode": "autonomous_live"})
        await db.governed_changes.insert_one({
            "user_id": uid, "field": "operational_mode",
            "new_value": "autonomous_live", "source": "mode_promotion",
            "status": "approved",
            "proposed_at": datetime.now(timezone.utc)})
        await remigrate_autonomous_to_supervised(db)
        g3 = await db.bot_configs.find_one(
            {"user_id": uid, "account_id": "g3"})
        assert g3["operational_mode"] == "autonomous_live"
        await db.bot_configs.delete_many({"user_id": uid})
        await db.governed_changes.delete_many({"user_id": uid})
        await db.platform_state.delete_one(
            {"_id": "mode_safety_remigration"})
    _run(go())


def test_record_swallow_counts_and_logs():
    before = swallow_counters().get("testcomp.testfn", 0)
    record_swallow("testcomp", "testfn", ValueError("boom"))
    record_swallow("testcomp", "testfn", ValueError("boom2"))
    assert swallow_counters()["testcomp.testfn"] == before + 2
