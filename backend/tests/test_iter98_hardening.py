"""iter-98 — safety hardening: observe default + grandfather migration,
promotion gate, material dual-approval, BSON datetimes, config versions."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from change_governance import material_change, snapshot_config
from operational_modes import (DEFAULT_MODE, evaluate_promotion,
                               migrate_default_modes, mode_gate,
                               record_intercept)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter98-{uuid.uuid4().hex[:8]}"


# ----------------------------------------------------- fail-safe default
def test_default_mode_is_observe():
    assert DEFAULT_MODE == "observe"
    mg = mode_gate({}, {})
    assert mg["mode"] == "observe" and not mg["allow_new"]


def test_grandfather_migration_idempotent(db):
    async def go():
        uid = f"{UID}-mig"
        await db.bot_configs.insert_many([
            {"user_id": uid, "account_id": "a1", "active": True},
            {"user_id": uid, "account_id": "a2", "active": False},
            {"user_id": uid, "account_id": "a3", "active": True,
             "operational_mode": "shadow"},
        ])
        await migrate_default_modes(db)
        by = {c["account_id"]: c async for c in
              db.bot_configs.find({"user_id": uid})}
        assert by["a1"]["operational_mode"] == "supervised_live"  # iter-103: no silent autonomy
        assert by["a2"]["operational_mode"] == "observe"          # fail-safe
        assert by["a3"]["operational_mode"] == "shadow"           # untouched
        # idempotent — second run touches nothing
        r2 = await migrate_default_modes(db)
        assert r2["active_grandfathered"] == 0
        assert r2["inactive_defaulted"] == 0
        await db.bot_configs.delete_many({"user_id": uid})
    _run(go())


# -------------------------------------------------------- promotion gate
def test_promotion_rules_degraded_blocks_live():
    certs = [{"account": "A", "tier": "DEGRADED"}]
    assert not evaluate_promotion("supervised_live", certs)["allowed"]
    assert not evaluate_promotion("autonomous_live", certs)["allowed"]
    # non-live targets are not certification-gated
    assert evaluate_promotion("demo_autopilot", certs)["allowed"]
    assert evaluate_promotion("shadow", certs)["allowed"]


def test_promotion_rules_provisional_capped_at_supervised():
    certs = [{"account": "A", "tier": "PROVISIONAL"}]
    sup = evaluate_promotion("supervised_live", certs)
    assert sup["allowed"] and sup["warnings"]
    auto = evaluate_promotion("autonomous_live", certs)
    assert not auto["allowed"]
    assert "PROVISIONAL" in auto["blockers"][0]


def test_promotion_rules_certified_full_autonomy():
    certs = [{"account": "A", "tier": "CERTIFIED"},
             {"account": "B", "tier": "ACCEPTABLE"}]
    v = evaluate_promotion("autonomous_live", certs)
    assert v["allowed"] and not v["warnings"]
    # one weak account poisons full autonomy
    v2 = evaluate_promotion("autonomous_live",
                            certs + [{"account": "C", "tier": "PROVISIONAL"}])
    assert not v2["allowed"]


# --------------------------------------------------- material thresholds
def test_material_change_rules():
    assert material_change("daily_drawdown_pct", 3.0, 3.1)
    assert material_change("max_leverage", 10, 20)
    assert material_change("kelly_enabled", False, True)
    assert material_change("operational_mode", "observe", "autonomous_live")
    assert material_change("risk_pct", 0.5, 0.75)          # exactly 1.5×
    assert not material_change("risk_pct", 0.5, 0.6)        # modest raise
    assert not material_change("operational_mode", "observe",
                               "supervised_live")


# --------------------------------------------------- BSON datetime writes
def test_new_writes_use_bson_datetimes(db):
    async def go():
        uid = f"{UID}-bson"
        from change_governance import propose_change
        await db.bot_configs.insert_one(
            {"user_id": uid, "account_id": "b1", "active": True,
             "risk_pct": 1.0})
        d = await propose_change(db, uid, "risk_pct", 1.0, 0.5, source="test")
        raw = await db.governed_changes.find_one({"_id": d["_id"]})
        assert isinstance(raw["proposed_at"], datetime)
        assert isinstance(raw["applied_at"], datetime)
        await record_intercept(db, uid, {"_id": "x"}, {"symbol": "XAUUSD"},
                               0.1, mode_gate({"operational_mode": "observe"}, {}))
        mi = await db.mode_intercepts.find_one({"user_id": uid})
        assert isinstance(mi["at"], datetime)
        await db.bot_configs.delete_many({"user_id": uid})
        await db.governed_changes.delete_many({"user_id": uid})
        await db.mode_intercepts.delete_many({"user_id": uid})
        await db.config_versions.delete_many({"user_id": uid})
    _run(go())


# ------------------------------------------------ immutable config versions
def test_snapshot_config_immutable_version(db):
    async def go():
        uid = f"{UID}-ver"
        await db.bot_configs.insert_one(
            {"user_id": uid, "account_id": "v1", "active": True,
             "risk_pct": 0.8, "min_confidence_override": 60})
        vid = await snapshot_config(db, uid, None, "test-snapshot")
        from bson import ObjectId
        doc = await db.config_versions.find_one({"_id": ObjectId(vid)})
        assert doc["config"]["risk_pct"] == 0.8
        assert len(doc["config_hash"]) == 64
        assert isinstance(doc["created_at"], datetime)
        # mutating the live config does not touch the version
        await db.bot_configs.update_one({"user_id": uid},
                                        {"$set": {"risk_pct": 2.0}})
        doc2 = await db.config_versions.find_one({"_id": ObjectId(vid)})
        assert doc2["config"]["risk_pct"] == 0.8
        await db.bot_configs.delete_many({"user_id": uid})
        await db.config_versions.delete_many({"user_id": uid})
    _run(go())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
