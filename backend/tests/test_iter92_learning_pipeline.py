"""iter-92 — Phase 5 continuous-learning pipeline: freeze guard, staged
candidate→holdout validation→approval→production, versioned rollbacks."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from motor.motor_asyncio import AsyncIOMotorClient

from learning_pipeline import (_snapshot_version, freeze_check, gated_retrain,
                               pipeline_status, staged_ml_retrain)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter92-{uuid.uuid4().hex[:8]}"


async def _cleanup(db):
    for coll in ("trades", "bot_configs", "learning_runs", "model_versions",
                 "rl_policies", "bayes_models", "online_learning"):
        await getattr(db, coll).delete_many({"user_id": UID})


async def _seed_trades(db, pnls, hours_ago_start=1):
    for i, p in enumerate(pnls):
        await db.trades.insert_one({
            "user_id": UID, "origin": "auto", "status": "closed",
            "pnl": float(p), "symbol": "XAUUSD", "action": "BUY",
            "entry_price": 4000.0, "stop_loss": 3990.0,
            "closed_at": (datetime.now(timezone.utc)
                          - timedelta(hours=hours_ago_start + i)).isoformat(),
            "opened_at": (datetime.now(timezone.utc)
                          - timedelta(hours=hours_ago_start + i + 1)).isoformat()})


# ------------------------------------------------------------ freeze guard
def test_freeze_on_losing_streak(db):
    async def run():
        await _cleanup(db)
        await _seed_trades(db, [-10, -20, -15, -30, -25])  # 5 straight losses
        frz = await freeze_check(db, UID)
        assert frz["frozen"] is True
        assert "losing streak" in frz["reason"]
        await _cleanup(db)
    _run(run())


def test_no_freeze_when_mixed_or_old(db):
    async def run():
        await _cleanup(db)
        await _seed_trades(db, [-10, 20, -15, -30, -25])   # a win breaks it
        assert (await freeze_check(db, UID))["frozen"] is False
        await _cleanup(db)
        # 5 losses but stale (>24h old) → gate open again
        await _seed_trades(db, [-10, -20, -15, -30, -25], hours_ago_start=30)
        assert (await freeze_check(db, UID))["frozen"] is False
        await _cleanup(db)
    _run(run())


def test_freeze_on_tripped_breaker(db):
    async def run():
        await _cleanup(db)
        await db.bot_configs.insert_one({
            "user_id": UID, "active": False,
            "tripped_at": datetime.now(timezone.utc).isoformat()})
        frz = await freeze_check(db, UID)
        assert frz["frozen"] is True
        assert "breaker" in frz["reason"]
        await _cleanup(db)
    _run(run())


# ------------------------------------------------------------ staged flow
def test_staged_retrain_insufficient_data(db):
    async def run():
        await _cleanup(db)
        await _seed_trades(db, [10, -5, 20])
        res = await staged_ml_retrain(db, UID)
        assert res["status"] == "insufficient_data"
        assert res["stage"] == "replay"
        await _cleanup(db)
    _run(run())


def test_gated_retrain_frozen_skips_all_training(db):
    async def run():
        await _cleanup(db)
        await _seed_trades(db, [-10, -20, -15, -30, -25])
        run_doc = await gated_retrain(db, UID, trigger="test")
        assert run_doc["frozen"] is True
        assert "ml_ensemble" not in run_doc["stages"]   # nothing trained
        stored = await db.learning_runs.find_one({"user_id": UID})
        assert stored and stored["frozen"] is True      # auditable
        # models untouched
        assert await db.rl_policies.find_one({"user_id": UID}) is None
        await _cleanup(db)
    _run(run())


def test_gated_retrain_open_gate_runs_stages(db):
    async def run():
        await _cleanup(db)
        await _seed_trades(db, [50, -10, 30, 25, -5, 40])  # mixed → open
        run_doc = await gated_retrain(db, UID, trigger="test-open")
        assert run_doc["frozen"] is False
        assert run_doc["stages"]["ml_ensemble"]["status"] == "insufficient_data"
        assert "rl_policy" in run_doc["stages"]
        assert "bayes" in run_doc["stages"]
        await _cleanup(db)
    _run(run())


def test_version_snapshot_rollback_cap(db):
    async def run():
        await _cleanup(db)
        await db.rl_policies.insert_one({"user_id": UID, "q": {"a": 1}})
        for _ in range(7):
            await _snapshot_version(db, UID, "rl_policy", "rl_policies")
        n = await db.model_versions.count_documents(
            {"user_id": UID, "model": "rl_policy"})
        assert n == 5  # capped
        v = await db.model_versions.find_one({"user_id": UID})
        assert v["doc"]["q"] == {"a": 1}
        await _cleanup(db)
    _run(run())


# ------------------------------------------------------------ status API
def test_pipeline_status_shape(db):
    async def run():
        await _cleanup(db)
        await gated_retrain(db, UID, trigger="status-test")
        st = await pipeline_status(db, UID)
        assert st["workflow"] == ["live_trades", "replay", "shadow",
                                  "validation", "approval", "production"]
        assert st["freeze"]["frozen"] is False
        assert len(st["recent_runs"]) == 1
        assert isinstance(st["recent_runs"][0]["at"], str)  # serializable
        await _cleanup(db)
    _run(run())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
