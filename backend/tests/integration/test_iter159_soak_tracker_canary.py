"""iter-159 — 14-day Soak Tracker + Release Canary Mode.

1. countdown_info: day / days-remaining / today's-checkpoint math (pure).
2. tracker_sweep: reminder alert at ≥12h into the day, safety-net
   auto-checkpoint at ≥20h, auto-start on release change, no-op otherwise.
3. reset: aborts the RUNNING campaign and starts a fresh one.
4. release canary: enable requires a DEMO/PAPER account; guard-block-rate
   divergence auto-halts (bot configs deactivated + critical alert);
   resume reactivates.
5. command_center: canary section GREEN when off, RED when halted.
"""
import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

pytestmark = pytest.mark.integration

DB_NAME = f"stoic_test_soaktrk_{uuid.uuid4().hex[:8]}"


def _now_dt():
    return datetime.now(timezone.utc)


def _fresh_db():
    os.environ["DB_NAME"] = DB_NAME
    import database
    database._client = None
    from database import get_db
    return get_db()


def test_countdown_info_pure():
    from soak_campaign import countdown_info
    now = _now_dt()
    camp = {"started_at": (now - timedelta(days=2, hours=13)).isoformat(),
            "days": 14}
    info = countdown_info(camp, [{"day": 1}, {"day": 2}], now=now)
    assert info["day"] == 3
    assert info["days_target"] == 14
    assert info["today_checkpoint_done"] is False
    assert info["hours_into_day"] == 13.0
    assert info["missed_days"] == []
    assert 11.0 < info["days_remaining"] < 11.6

    done = countdown_info(camp, [{"day": 1}, {"day": 2}, {"day": 3}],
                          now=now)
    assert done["today_checkpoint_done"] is True

    gap = countdown_info(camp, [{"day": 1}], now=now)
    assert gap["missed_days"] == [2]


async def _tracker_scenario():
    db = _fresh_db()
    from soak_campaign import start, tracker_sweep
    from soak_campaign import status as soak_status
    try:
        # no campaign ever → NO auto-start (first campaign is a human call)
        out = await tracker_sweep(db)
        assert out == {"auto_started": False, "reminded": False,
                       "auto_checkpoint": False}
        assert await db.soak_campaigns.count_documents({}) == 0

        # 13h into day 1, checkpoint missing → reminder alert (deduped)
        camp = await start(db, "tester")
        await db.soak_campaigns.update_one(
            {"campaign_id": camp["campaign_id"]},
            {"$set": {"started_at":
                      (_now_dt() - timedelta(hours=13)).isoformat()}})
        out = await tracker_sweep(db)
        assert out["reminded"] is True
        alert = await db.ops_alerts.find_one({"kind": "soak_checkpoint_due"})
        assert alert and alert["acked_at"] is None

        # status() now carries the countdown block
        st = await soak_status(db)
        assert st["countdown"]["day"] == 1
        assert st["countdown"]["today_checkpoint_done"] is False

        # 21h into the day → safety-net auto checkpoint, coverage kept
        await db.soak_campaigns.update_one(
            {"campaign_id": camp["campaign_id"]},
            {"$set": {"started_at":
                      (_now_dt() - timedelta(hours=21)).isoformat()}})
        out = await tracker_sweep(db)
        assert out["auto_checkpoint"] is True
        cp = await db.soak_checkpoints.find_one(
            {"campaign_id": camp["campaign_id"], "day": 1})
        assert cp["recorded_by"] == "auto"

        # checkpoint recorded → sweep is a no-op
        out = await tracker_sweep(db)
        assert out == {"auto_started": False, "reminded": False,
                       "auto_checkpoint": False}

        # finished campaign frozen on an OLD release → auto-start
        await db.soak_campaigns.update_one(
            {"campaign_id": camp["campaign_id"]},
            {"$set": {"status": "PASS",
                      "frozen_versions.release": "old-release"}})
        out = await tracker_sweep(db)
        assert out["auto_started"] is True
        fresh = await db.soak_campaigns.find_one({"status": "RUNNING"})
        assert fresh["started_by"] == "auto"
        assert "auto-started" in (fresh.get("note") or "")
        assert await db.ops_alerts.count_documents(
            {"kind": "soak_auto_started"}) == 1
    finally:
        await db.client.drop_database(DB_NAME)


def test_soak_tracker_sweep():
    asyncio.run(_tracker_scenario())


async def _reset_scenario():
    db = _fresh_db()
    from soak_campaign import reset, start
    try:
        camp = await start(db, "tester")
        res = await reset(db, "tester")
        assert res["aborted_campaign"] == camp["campaign_id"]
        assert res["status"] == "RUNNING"
        assert res["campaign_id"] != camp["campaign_id"]
        old = await db.soak_campaigns.find_one(
            {"campaign_id": camp["campaign_id"]})
        assert old["status"] == "ABORTED"
        assert old["abort_reason"] == "manual reset"
        # reset with nothing running still starts fresh
        await db.soak_campaigns.update_many(
            {}, {"$set": {"status": "ABORTED"}})
        res2 = await reset(db, "tester")
        assert res2["status"] == "RUNNING"
        assert res2["aborted_campaign"] is None
    finally:
        await db.client.drop_database(DB_NAME)


def test_soak_reset():
    asyncio.run(_reset_scenario())


def test_divergence_verdict_pure():
    from release_canary import divergence_verdict
    # insufficient evidence never halts
    v = divergence_verdict({"decisions": 5, "block_rate": 1.0},
                           {"decisions": 100, "block_rate": 0.0})
    assert v["diverged"] is False and v["judged"] is False
    # within threshold (fleet 10% → threshold 35%)
    v = divergence_verdict({"decisions": 30, "block_rate": 0.3},
                           {"decisions": 100, "block_rate": 0.1})
    assert v["diverged"] is False and v["judged"] is True
    # beyond threshold
    v = divergence_verdict({"decisions": 30, "block_rate": 0.8},
                           {"decisions": 100, "block_rate": 0.1})
    assert v["diverged"] is True
    # quiet fleet: threshold floors at +25pp
    v = divergence_verdict({"decisions": 30, "block_rate": 0.3},
                           {"decisions": 0, "block_rate": 0.0})
    assert v["diverged"] is True


async def _canary_scenario():
    db = _fresh_db()
    import release_canary as rc
    try:
        demo, live = ObjectId(), ObjectId()
        await db.accounts.insert_one(
            {"_id": demo, "display_name": "Canary Demo",
             "broker_environment": "DEMO"})
        await db.accounts.insert_one(
            {"_id": live, "display_name": "Real Money",
             "broker_environment": "LIVE"})

        with pytest.raises(ValueError):
            await rc.enable(db, str(live), "admin")

        st = await rc.enable(db, str(demo), "admin")
        assert st["enabled"] is True and st["environment"] == "DEMO"

        # insufficient evidence → judged False, no halt
        out = await rc.evaluate(db)
        assert out["halted"] is False
        assert out["verdict"]["judged"] is False

        # canary blocks 80% (20/25) vs fleet 10% (5/50) → auto-halt
        now = _now_dt().isoformat()
        docs = [{"snapshot_id": f"c{i}", "at": now,
                 "account_id": str(demo), "authorized": i % 5 == 0,
                 "reason": "risk_unknown"} for i in range(25)]
        docs += [{"snapshot_id": f"f{i}", "at": now,
                  "account_id": "fleet-acc", "authorized": i % 10 != 0,
                  "reason": "spread"} for i in range(50)]
        await db.pamm_risk_decisions.insert_many(docs)
        await db.bot_configs.insert_one(
            {"account_id": str(demo), "active": True, "user_id": "u1"})

        out = await rc.evaluate(db)
        assert out["halted"] is True
        assert out["configs_deactivated"] == 1
        cfg = await db.bot_configs.find_one({"account_id": str(demo)})
        assert cfg["active"] is False and cfg["canary_halted"] is True
        alert = await db.ops_alerts.find_one({"kind": "canary_halt"})
        assert alert and alert["severity"] == "critical"

        # halted canary is not re-evaluated
        out = await rc.evaluate(db)
        assert out["evaluated"] is False and out["halted"] is True

        res = await rc.resume(db, "admin")
        assert res["configs_reactivated"] == 1
        cfg = await db.bot_configs.find_one({"account_id": str(demo)})
        assert cfg["active"] is True and "canary_halted" not in cfg
        st = await rc.status(db)
        assert st["halted"] is False and st["enabled"] is True

        await rc.disable(db, "admin")
        st = await rc.status(db)
        assert st["enabled"] is False
    finally:
        await db.client.drop_database(DB_NAME)


def test_release_canary_lifecycle():
    asyncio.run(_canary_scenario())


async def _cc_scenario():
    db = _fresh_db()
    from command_center import status
    try:
        out = await status(db)
        assert out["sections"]["canary"]["status"] == "GREEN"
        assert out["sections"]["canary"]["enabled"] is False

        await db.platform_state.update_one(
            {"_id": "release_canary"},
            {"$set": {"enabled": True, "account_id": "acc1",
                      "halted": True,
                      "halt_reason": "block-rate divergence"}},
            upsert=True)
        out = await status(db)
        c = out["sections"]["canary"]
        assert c["status"] == "RED" and c["halted"] is True
        assert "HALTED" in c["detail"]
        assert out["overall"] == "RED"
    finally:
        await db.client.drop_database(DB_NAME)


def test_command_center_canary_section():
    asyncio.run(_cc_scenario())
