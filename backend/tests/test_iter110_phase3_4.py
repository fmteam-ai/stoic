"""iter-110 — Phase 3/4 Live-Ops: capital stages, subsystem conservatism,
operator intervention framework."""
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

from capital_stages import capital_stage, stage_risk_cap
from operator_actions import ACTIONS, run_action
from subsystem_health import conservatism_from_scores


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter110-{uuid.uuid4().hex[:8]}"


def _trades(uid, pnls):
    now = datetime.now(timezone.utc)
    return [{"user_id": uid, "status": "closed", "origin": "auto",
             "pnl": float(p), "risk_amount": 10.0,
             "closed_at": (now - timedelta(hours=i)).isoformat()}
            for i, p in enumerate(pnls)]


# ─── Capital stages ─────────────────────────────────────────────
def test_stage1_pilot_when_no_evidence(db):
    async def go():
        out = await capital_stage(db, f"{UID}-empty")
        assert out["stage"] == 1 and out["label"] == "PILOT"
        assert out["risk_cap_pct"] == 0.25
        assert out["evidence"]["n"] == 0
    _run(go())


def test_stage2_scale_at_100_trades(db):
    async def go():
        uid = f"{UID}-s2"
        # 80 wins +15, 40 losses -10 → PF 1.2 wins vs losses, DD small
        pnls = ([15.0] * 4 + [-10.0] * 2) * 20
        await db.trades.insert_many(_trades(uid, pnls))
        out = await capital_stage(db, uid)
        assert out["evidence"]["n"] == 120
        assert out["stage"] == 2 and out["label"] == "SCALE"
        assert out["risk_cap_pct"] == 0.5
        assert "300" in out["next_milestone"]
    _run(go())


def test_stage3_deploy_at_300_trades_positive_ci(db):
    async def go():
        uid = f"{UID}-s3"
        pnls = ([15.0] * 4 + [-10.0] * 1) * 64  # 320 trades, strongly +EV
        await db.trades.insert_many(_trades(uid, pnls))
        out = await capital_stage(db, uid)
        assert out["evidence"]["n"] == 320
        assert out["evidence"]["ci95_lower_r"] > 0
        assert out["stage"] == 3 and out["label"] == "DEPLOY"
        assert out["risk_cap_pct"] is None
    _run(go())


def test_stage_demotes_on_drawdown(db):
    async def go():
        uid = f"{UID}-dd"
        # 120 trades but a -25R drawdown streak → stays PILOT
        pnls = [15.0] * 60 + [-10.0] * 25 + [15.0] * 35
        await db.trades.insert_many(_trades(uid, pnls))
        out = await capital_stage(db, uid)
        assert out["evidence"]["max_drawdown_r"] > 20
        assert out["stage"] == 1 and out["risk_cap_pct"] == 0.25
    _run(go())


def test_stage_risk_cap_caches(db):
    async def go():
        uid = f"{UID}-cache"
        cap1, info1 = await stage_risk_cap(db, uid)
        assert cap1 == 0.25
        # insert qualifying trades — cached result must still be served
        await db.trades.insert_many(_trades(uid, ([15.0] * 4 + [-10.0]) * 30))
        cap2, info2 = await stage_risk_cap(db, uid)
        assert cap2 == 0.25 and info2["stage"] == info1["stage"]
    _run(go())


# ─── Subsystem conservatism (pure) ──────────────────────────────
def test_conservatism_all_healthy():
    out = conservatism_from_scores(
        {"learning_engine": 90, "execution_engine": 75, "risk_engine": 80})
    assert out["multiplier"] == 1.0
    assert out["worst"] == "execution_engine"


def test_conservatism_degraded_subsystem():
    out = conservatism_from_scores(
        {"learning_engine": 90, "execution_engine": 45})
    assert out["multiplier"] == 0.75
    assert out["worst"] == "execution_engine"
    assert "execution_engine" in out["reason"]


def test_conservatism_critical_subsystem():
    out = conservatism_from_scores({"broker_engine": 20, "risk_engine": 90})
    assert out["multiplier"] == 0.5
    assert out["worst"] == "broker_engine"


def test_conservatism_ignores_unknown_scores():
    out = conservatism_from_scores({"a": None, "b": None})
    assert out["multiplier"] == 1.0 and out["worst"] is None


# ─── Operator actions ───────────────────────────────────────────
def _cfg(uid, **kw):
    base = {"user_id": uid, "active": True, "risk_pct": 1.0,
            "operational_mode": "autonomous",
            "symbols": ["XAUUSD", "BTCUSD"], "max_concurrent_trades": 3,
            "account_id": f"acc-{uuid.uuid4().hex[:6]}"}
    base.update(kw)
    return base


def test_freeze_trading_sets_observe(db):
    async def go():
        uid = f"{UID}-freeze"
        await db.bot_configs.insert_many([_cfg(uid), _cfg(uid)])
        out = await run_action(db, uid, "freeze_trading")
        assert "2 config(s)" in out["detail"]
        n = await db.bot_configs.count_documents(
            {"user_id": uid, "operational_mode": "observe"})
        assert n == 2
        audit = await db.audit_log.find_one(
            {"user_id": uid, "action": "operator_action:freeze_trading"})
        assert audit is not None
    _run(go())


def test_reduce_exposure_halves_with_floor(db):
    async def go():
        uid = f"{UID}-reduce"
        await db.bot_configs.insert_many(
            [_cfg(uid, risk_pct=1.0), _cfg(uid, risk_pct=0.06)])
        await run_action(db, uid, "reduce_exposure")
        risks = sorted([c["risk_pct"] async for c in
                        db.bot_configs.find({"user_id": uid})])
        assert risks == [0.05, 0.5]
    _run(go())


def test_defensive_mode_limits_concurrency(db):
    async def go():
        uid = f"{UID}-def"
        await db.bot_configs.insert_one(_cfg(uid, risk_pct=0.8))
        await run_action(db, uid, "defensive_mode")
        cfg = await db.bot_configs.find_one({"user_id": uid})
        assert cfg["risk_pct"] == 0.4
        assert cfg["max_concurrent_trades"] == 1
    _run(go())


def test_pause_symbol_removes_symbol(db):
    async def go():
        uid = f"{UID}-pause"
        await db.bot_configs.insert_one(_cfg(uid))
        await run_action(db, uid, "pause_symbol", {"symbol": "xauusd"})
        cfg = await db.bot_configs.find_one({"user_id": uid})
        assert cfg["symbols"] == ["BTCUSD"]
    _run(go())


def test_pause_symbol_requires_symbol(db):
    async def go():
        with pytest.raises(ValueError):
            await run_action(db, f"{UID}-nosym", "pause_symbol", {})
    _run(go())


def test_panic_mode_freezes_all_accounts(db):
    async def go():
        uid = f"{UID}-panic"
        await db.bot_configs.insert_many(
            [_cfg(uid, account_id="a1"), _cfg(uid, account_id="a2"),
             _cfg(uid, account_id="a3")])
        out = await run_action(db, uid, "panic_mode",
                               {"account_id": "a1"})  # scope ignored
        assert "PANIC" in out["detail"]
        n = await db.bot_configs.count_documents(
            {"user_id": uid, "operational_mode": "observe"})
        assert n == 3
    _run(go())


def test_unknown_action_rejected(db):
    async def go():
        with pytest.raises(ValueError):
            await run_action(db, f"{UID}-bad", "raise_risk")
    _run(go())


def test_action_scoped_to_account(db):
    async def go():
        uid = f"{UID}-scope"
        await db.bot_configs.insert_many(
            [_cfg(uid, account_id="target"), _cfg(uid, account_id="other")])
        await run_action(db, uid, "freeze_trading", {"account_id": "target"})
        target = await db.bot_configs.find_one(
            {"user_id": uid, "account_id": "target"})
        other = await db.bot_configs.find_one(
            {"user_id": uid, "account_id": "other"})
        assert target["operational_mode"] == "observe"
        assert other["operational_mode"] == "autonomous"
    _run(go())


def test_actions_registry_complete():
    assert set(ACTIONS) == {"freeze_trading", "reduce_exposure",
                            "pause_symbol", "defensive_mode", "panic_mode"}


# ─── Cleanup ────────────────────────────────────────────────────
def test_zz_cleanup(db):
    async def go():
        await db.trades.delete_many({"user_id": {"$regex": f"^{UID}"}})
        await db.bot_configs.delete_many({"user_id": {"$regex": f"^{UID}"}})
        await db.audit_log.delete_many({"user_id": {"$regex": f"^{UID}"}})
    _run(go())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
