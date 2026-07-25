"""iter-104 — immutable config promotion: versions, pointer, rollback."""
import asyncio
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from config_promotion import _pointer_id, record_version, rollback


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter104-{uuid.uuid4().hex[:8]}"
ACC = "cv-acc"


async def _cleanup(db, uid):
    for c in ("bot_configs", "config_versions", "config_pointers",
              "audit_log"):
        await getattr(db, c).delete_many({"user_id": uid})


def test_record_version_advances_pointer_and_dedups(db):
    async def go():
        uid = f"{UID}-a"
        cfg1 = {"user_id": uid, "account_id": ACC, "risk_pct": 0.5,
                "operational_mode": "supervised_live"}
        await db.bot_configs.insert_one(dict(cfg1))
        v1 = await record_version(db, cfg1, "v1", "test")
        assert v1
        # same content → dedup, no new version
        v1b = await record_version(db, cfg1, "v1-again", "test")
        assert v1b == v1
        cfg2 = {**cfg1, "risk_pct": 0.3}
        v2 = await record_version(db, cfg2, "v2", "test")
        assert v2 != v1
        ptr = await db.config_pointers.find_one(
            {"_id": _pointer_id(uid, ACC)})
        assert ptr["active_version_id"] == v2
        assert ptr["previous_version_id"] == v1
        await _cleanup(db, uid)
    _run(go())


def test_rollback_swaps_pointer_and_reapplies(db):
    async def go():
        uid = f"{UID}-b"
        cfg1 = {"user_id": uid, "account_id": ACC, "risk_pct": 0.5,
                "operational_mode": "supervised_live"}
        await db.bot_configs.insert_one(dict(cfg1))
        v1 = await record_version(db, cfg1, "v1", "test")
        cfg2 = {**cfg1, "risk_pct": 0.9}
        await db.bot_configs.update_one(
            {"user_id": uid, "account_id": ACC},
            {"$set": {"risk_pct": 0.9}})
        v2 = await record_version(db, cfg2, "v2", "test")
        res = await rollback(db, uid, ACC, actor="tester")
        assert res["rolled_back_to"] == v1
        assert res["roll_forward_version_id"] == v2
        cur = await db.bot_configs.find_one(
            {"user_id": uid, "account_id": ACC})
        assert cur["risk_pct"] == 0.5
        ptr = await db.config_pointers.find_one(
            {"_id": _pointer_id(uid, ACC)})
        assert ptr["active_version_id"] == v1
        assert ptr["previous_version_id"] == v2   # roll-forward possible
        await _cleanup(db, uid)
    _run(go())


def test_rollback_never_raises_operational_mode(db):
    async def go():
        uid = f"{UID}-c"
        cfg1 = {"user_id": uid, "account_id": ACC, "risk_pct": 0.5,
                "operational_mode": "autonomous_live"}
        await db.bot_configs.insert_one(dict(cfg1))
        await record_version(db, cfg1, "v1-autonomous", "test")
        # operator demoted to supervised_live afterwards
        cfg2 = {**cfg1, "operational_mode": "supervised_live"}
        await db.bot_configs.update_one(
            {"user_id": uid, "account_id": ACC},
            {"$set": {"operational_mode": "supervised_live"}})
        await record_version(db, cfg2, "v2-supervised", "test")
        res = await rollback(db, uid, ACC, actor="tester")
        assert res["mode_guard_applied"] is True
        cur = await db.bot_configs.find_one(
            {"user_id": uid, "account_id": ACC})
        # rollback restored v1 content BUT kept the demoted mode
        assert cur["operational_mode"] == "supervised_live"
        await _cleanup(db, uid)
    _run(go())


def test_rollback_without_history_raises(db):
    async def go():
        with pytest.raises(ValueError):
            await rollback(db, f"nobody-{UID}", ACC)
    _run(go())
