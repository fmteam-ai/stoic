"""iter-111 — Six safety corrections: fail-closed health, per-account
broker stability, per-feed freshness, atomic config promotion, broker
certification completeness, automatic mode demotion ladder."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from auto_demotion import evaluate_ceiling, recovery_status, sweep_user
from broker_qualification import qualify_account
from config_promotion import (apply_config_change,
                              repair_incomplete_promotions)
from shadow_health import (CRITICAL_COMPONENTS, THRESHOLD,
                           fail_closed_aggregate, health_score)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter111-{uuid.uuid4().hex[:8]}"
NOW = datetime.now(timezone.utc)


# ─── #1 fail-closed health ──────────────────────────────────────
def test_fail_closed_aggregate_missing_critical():
    raw = {"data_freshness": 90, "regime_confidence": 80,
           "calibration_quality": 70, "execution_quality": 85,
           "broker_stability": None, "worker_health": 95,
           "synchronization": 88}
    agg = fail_closed_aggregate(raw)
    assert agg["fail_closed"] is True
    assert agg["missing"] == ["broker_stability"]
    assert agg["effective"]["broker_stability"] == 0
    # a single missing critical metric can no longer look healthy
    assert agg["overall"] < 75


def test_fail_closed_aggregate_soft_missing_is_penalized_not_fatal():
    raw = {"data_freshness": 90, "regime_confidence": None,
           "calibration_quality": None, "execution_quality": 85,
           "broker_stability": 90, "worker_health": 95,
           "synchronization": 88}
    agg = fail_closed_aggregate(raw)
    assert agg["fail_closed"] is False
    assert agg["effective"]["regime_confidence"] == 40
    assert agg["effective"]["calibration_quality"] == 40


def test_fail_closed_aggregate_flags_stale():
    raw = {"data_freshness": 15, "broker_stability": 100}
    agg = fail_closed_aggregate(raw)
    assert agg["stale"] == ["data_freshness"]
    assert agg["missing"] == []


def test_health_score_missing_critical_pauses_promotions(db):
    async def go():
        out = await health_score(db, f"{UID}-empty")
        # user has no accounts → broker_stability unknown → fail closed
        assert "broker_stability" in out["missing_components"]
        assert out["fail_closed"] is True
        assert out["promotions_paused"] is True
        assert out["components"]["broker_stability"] == 0
        assert set(out["components"]) == {
            "data_freshness", "regime_confidence", "calibration_quality",
            "execution_quality", "broker_stability", "worker_health",
            "synchronization"}
    _run(go())


# ─── #2 per-account broker stability ────────────────────────────
def test_worst_active_account_drives_stability(db):
    async def go():
        uid = f"{UID}-worst"
        await db.accounts.insert_many([
            {"user_id": uid, "mode": "live", "label": "fresh",
             "bridge_token": f"tok-{uuid.uuid4().hex}",
             "last_heartbeat": NOW.isoformat()},
            {"user_id": uid, "mode": "live", "label": "stale",
             "bridge_token": f"tok-{uuid.uuid4().hex}",
             "last_heartbeat": (NOW - timedelta(hours=2)).isoformat()}])
        out = await health_score(db, uid)
        assert out["components"]["broker_stability"] <= 15
        accts = out["details"]["broker_stability_accounts"]
        assert len(accts) == 2
        by_label = {a["label"]: a for a in accts}
        assert by_label["fresh"]["score"] == 100
        assert by_label["stale"]["score"] <= 15
    _run(go())


def test_live_account_without_heartbeat_scores_zero(db):
    async def go():
        uid = f"{UID}-nohb"
        await db.accounts.insert_one(
            {"user_id": uid, "mode": "live", "label": "never-connected",
             "bridge_token": f"tok-{uuid.uuid4().hex}"})
        out = await health_score(db, uid)
        assert out["components"]["broker_stability"] == 0
        assert out["components"]["synchronization"] == 0
    _run(go())


# ─── #3 per-feed freshness ──────────────────────────────────────
def test_worst_required_feed_drives_freshness(db):
    async def go():
        uid = f"{UID}-feeds"
        await db.bot_configs.insert_one(
            {"user_id": uid, "active": True,
             "symbols": ["XAUUSD", "BTCUSD"],
             "operational_mode": "observe"})
        await db.intraday_candles.insert_one(
            {"user_id": uid, "symbol": "XAUUSD",
             "bars": [{"t": NOW.timestamp() - 60, "o": 1, "h": 1,
                       "l": 1, "c": 1}]})
        out = await health_score(db, uid)
        feeds = out["details"]["data_freshness_feeds"]
        assert set(feeds) == {"XAUUSD", "BTCUSD"}
        assert feeds["XAUUSD"]["score"] == 100
        assert feeds["BTCUSD"]["score"] is None  # no data at all
        # worst required feed (missing → 0) drives the component
        assert out["components"]["data_freshness"] == 0
    _run(go())


# ─── #4 atomic promotion ────────────────────────────────────────
def test_apply_config_change_is_consistent(db):
    async def go():
        uid = f"{UID}-atomic"
        await db.bot_configs.insert_one(
            {"user_id": uid, "active": True, "risk_pct": 1.0,
             "operational_mode": "observe"})
        vid = await apply_config_change(
            db, uid, None, {"risk_pct": 0.7}, label="test",
            source="test")
        cfg = await db.bot_configs.find_one({"user_id": uid})
        assert cfg["risk_pct"] == 0.7
        ptr = await db.config_pointers.find_one({"_id": f"{uid}:default"})
        assert ptr["active_version_id"] == vid
        from bson import ObjectId
        ver = await db.config_versions.find_one({"_id": ObjectId(vid)})
        assert ver["config_hash"] == ptr["active_hash"]
        assert ver["config"]["risk_pct"] == 0.7
        # journal completed + audit event written
        assert await db.promotion_journal.count_documents(
            {"user_id": uid, "status": "in_progress"}) == 0
        assert await db.audit_log.find_one(
            {"user_id": uid, "action": "config_change_applied"})
    _run(go())


def test_startup_repair_converges_crashed_change(db):
    async def go():
        uid = f"{UID}-repair"
        await db.bot_configs.insert_one(
            {"user_id": uid, "active": True, "risk_pct": 1.0,
             "operational_mode": "observe"})
        # simulate a crash: journal written, config/pointer never updated
        await db.promotion_journal.insert_one(
            {"user_id": uid, "account_id": None,
             "update": {"risk_pct": 0.4}, "label": "crashed",
             "source": "test", "status": "in_progress",
             "at": NOW - timedelta(minutes=5)})
        out = await repair_incomplete_promotions(db)
        assert out["repaired"] >= 1
        cfg = await db.bot_configs.find_one({"user_id": uid})
        assert cfg["risk_pct"] == 0.4
        ptr = await db.config_pointers.find_one({"_id": f"{uid}:default"})
        assert ptr is not None and ptr["active_version_id"]
        j = await db.promotion_journal.find_one({"user_id": uid})
        assert j["status"] == "repaired"
    _run(go())


# ─── #5 broker certification completeness ───────────────────────
async def _seed_qualified_account(db, uid, label, with_specs):
    res = await db.accounts.insert_one({
        "user_id": uid, "mode": "live", "label": label,
        "broker": "TestBroker", "server": "Test-Live",
        "account_type": "hedging",
        "bridge_token": f"tok-{uuid.uuid4().hex}",
        **({"symbol_specs": {"XAUUSD": {
                "point": 0.01, "digits": 2, "stops_level_points": 10,
                "freeze_level_points": 0, "trade_mode": 4,
                "tick_size": 0.01, "tick_value": 1.0,
                "contract_size": 100.0, "volume_min": 0.01,
                "volume_max": 100.0, "volume_step": 0.01}},
            "broker_time_info": {
                "server_gmt_offset_sec": 7200,
                "trade_sessions_today": [[60, 86340]]}}
           if with_specs else {})})
    acc_id = str(res.inserted_id)
    await db.broker_deals.insert_many([
        {"account_id": acc_id, "deal_entry": "in", "mt5_ticket": i,
         "deal_id": f"{uuid.uuid4().hex[:12]}", "commission": 0,
         "lots": 0.1} for i in range(25)])
    await db.broker_intel_scores.insert_one(
        {"account_id": acc_id, "score": 85,
         "components": {"slippage": 90, "latency": 88, "spread": 80},
         "at": NOW})
    return await db.accounts.find_one({"_id": res.inserted_id})


def test_certified_requires_full_spec_report(db):
    async def go():
        uid = f"{UID}-cert"
        acc = await _seed_qualified_account(db, uid, "complete", True)
        cert = await qualify_account(db, acc)
        assert cert["tier"] == "CERTIFIED", cert["detail"]
        for k in ("stop_restrictions", "freeze_levels", "symbol_specs",
                  "dst_handling"):
            assert cert["checks"][k]["status"] == "observed", k
    _run(go())


def test_certified_withheld_without_specs(db):
    async def go():
        uid = f"{UID}-nospec"
        acc = await _seed_qualified_account(db, uid, "incomplete", False)
        cert = await qualify_account(db, acc)
        assert cert["tier"] == "ACCEPTABLE"
        assert "CERTIFIED withheld" in cert["detail"]
        assert "v1.54" in cert["detail"]
    _run(go())


# ─── #6 auto-demotion ladder ────────────────────────────────────
def _health(overall, broker=80, sync=80):
    return {"overall": overall,
            "components": {"broker_stability": broker,
                           "synchronization": sync}}


def test_ceiling_healthy_no_demotion():
    v = evaluate_ceiling(_health(85), sustained_below_threshold=False)
    assert v["ceiling"] is None


def test_ceiling_sustained_deterioration_supervised():
    v = evaluate_ceiling(_health(55), sustained_below_threshold=True)
    assert v["ceiling"] == "supervised_live"
    # a single bad sample (not sustained) does not demote
    v2 = evaluate_ceiling(_health(55), sustained_below_threshold=False)
    assert v2["ceiling"] is None


def test_ceiling_severe_deterioration_defensive():
    v = evaluate_ceiling(_health(30), sustained_below_threshold=True)
    assert v["ceiling"] == "defensive"


def test_ceiling_broker_uncertainty_freezes():
    v = evaluate_ceiling(_health(90, broker=0), False)
    assert v["ceiling"] == "observe"
    v2 = evaluate_ceiling(_health(90, sync=None), False)
    assert v2["ceiling"] == "observe"


def test_sweep_demotes_autonomous_on_broker_uncertainty(db):
    async def go():
        uid = f"{UID}-sweep"
        # no accounts → broker_stability fails closed → ceiling observe
        await db.bot_configs.insert_one(
            {"user_id": uid, "active": True, "risk_pct": 0.5,
             "operational_mode": "autonomous_live",
             "symbols": ["XAUUSD"]})
        out = await sweep_user(db, uid)
        assert out["verdict"]["ceiling"] == "observe"
        assert len(out["demoted"]) == 1
        cfg = await db.bot_configs.find_one({"user_id": uid})
        assert cfg["operational_mode"] == "observe"
        assert cfg["mode_demotion_reason"]
        dem = await db.mode_demotions.find_one({"user_id": uid})
        assert dem["automatic"] is True
        assert await db.audit_log.find_one(
            {"user_id": uid, "action": "auto_mode_demotion"})
        assert await db.health_samples.find_one({"user_id": uid})
    _run(go())


def test_recovery_locked_after_recent_demotion(db):
    async def go():
        uid = f"{UID}-sweep"  # reuse the demoted user above
        rec = await recovery_status(db, uid)
        assert rec["applicable"] is True
        assert rec["eligible"] is False
        assert "green period" in rec["reason"]
    _run(go())


def test_recovery_eligible_after_green_period(db):
    async def go():
        uid = f"{UID}-green"
        await db.mode_demotions.insert_one(
            {"user_id": uid, "automatic": True, "reason": "test",
             "ceiling": "observe", "demotions": [],
             "at": NOW - timedelta(hours=25)})
        await db.health_samples.insert_many([
            {"user_id": uid, "overall": 80, "fail_closed": False,
             "at": NOW - timedelta(hours=h)} for h in range(12)])
        rec = await recovery_status(db, uid)
        assert rec["eligible"] is True
        assert "explicit" in rec["reason"]
    _run(go())


def test_promotion_gate_blocks_during_recovery(db):
    async def go():
        from operational_modes import promotion_gate
        uid = f"{UID}-sweep"  # demoted user with no green period
        verdict = await promotion_gate(db, uid, None, "autonomous_live")
        assert verdict["allowed"] is False
        assert any("recovery" in b for b in verdict["blockers"])
    _run(go())


# ─── Cleanup ────────────────────────────────────────────────────
def test_zz_cleanup(db):
    async def go():
        rx = {"$regex": f"^{UID}"}
        acc_ids = [str(a["_id"]) async for a in
                   db.accounts.find({"user_id": rx}, {"_id": 1})]
        for coll in ("bot_configs", "accounts", "intraday_candles",
                     "audit_log", "mode_demotions", "health_samples",
                     "promotion_journal", "config_versions"):
            await db[coll].delete_many({"user_id": rx})
        await db.config_pointers.delete_many({"user_id": rx})
        await db.broker_deals.delete_many({"account_id": {"$in": acc_ids}})
        await db.broker_intel_scores.delete_many(
            {"account_id": {"$in": acc_ids}})
        await db.broker_certifications.delete_many(
            {"account_id": {"$in": acc_ids}})
        await db.ops_alerts.delete_many(
            {"dedup_key": {"$regex": f"^auto_demotion_{UID}"}})
    _run(go())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
