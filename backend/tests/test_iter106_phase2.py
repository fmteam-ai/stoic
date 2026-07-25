"""iter-106 — Phase 2 Demo Readiness: broker qualification, soak report,
new chaos drills, statistical promotion validation."""
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

from chaos_drills import run_drills
from statistical_validation import (MAX_DD_R, MIN_TRADES,
                                    promotion_evidence)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter106-{uuid.uuid4().hex[:8]}"


def test_chaos_now_runs_eight_drills(db):
    async def go():
        out = await run_drills(db)
        assert out["total"] == 8
        names = {r["drill"] for r in out["results"]}
        assert {"api_outage", "clock_skew", "alert_storm_dedup"} <= names
        by = {r["drill"]: r for r in out["results"]}
        assert by["clock_skew"]["passed"] is True
        assert by["alert_storm_dedup"]["passed"] is True
        assert by["api_outage"]["passed"] is True
        assert await db.ops_alerts.count_documents(
            {"dedup_key": {"$regex": "^chaos_dedup_"}}) == 0
    _run(go())


def test_statistical_validation_blocks_thin_evidence(db):
    async def go():
        uid = f"{UID}-thin"
        now = datetime.now(timezone.utc).isoformat()
        await db.trades.insert_many([
            {"user_id": uid, "status": "closed", "origin": "auto",
             "pnl": 10.0, "risk_amount": 10.0, "closed_at": now}
            for _ in range(5)])
        ev = await promotion_evidence(db, uid)
        assert ev["sufficient"] is False
        assert ev["checks"]["sample_size"]["pass"] is False
        assert any("sample" in b or f"{MIN_TRADES}" in b
                   for b in ev["blockers"])
        await db.trades.delete_many({"user_id": uid})
    _run(go())


def test_statistical_validation_uses_ci_lower_bound(db):
    async def go():
        uid = f"{UID}-ci"
        now = datetime.now(timezone.utc).isoformat()
        # high variance, small positive mean → CI lower bound < 0 → blocked
        docs = []
        for i in range(320):
            pnl = 50.0 if i % 2 else -49.0
            docs.append({"user_id": uid, "status": "closed",
                         "origin": "auto", "pnl": pnl, "risk_amount": 49.0,
                         "closed_at": now, "confidence": 70,
                         "scope": "hf_scalp"})
        await db.trades.insert_many(docs)
        ev = await promotion_evidence(db, uid)
        assert ev["checks"]["sample_size"]["pass"] is True
        assert ev["mean_r"] > 0
        assert ev["checks"]["expectancy_ci"]["pass"] == (
            ev["ci95_lower_r"] > 0)
        assert ev["max_drawdown_r"] <= MAX_DD_R
        await db.trades.delete_many({"user_id": uid})
    _run(go())


def test_broker_qualification_matrix(db):
    async def go():
        from broker_qualification import qualify_account
        uid = f"{UID}-bq"
        acc_id = (await db.accounts.insert_one({
            "user_id": uid, "label": "QualTest", "broker": "TestBroker",
            "server": "Test-Live", "account_type": "netting",
            "bridge_token": f"qual-{uuid.uuid4().hex}",
            "status": "active"})).inserted_id
        for i in range(25):
            await db.broker_deals.insert_one({
                "account_id": str(acc_id), "deal_id": f"q{UID}{i}",
                "deal_entry": "in", "mt5_ticket": 9000 + i,
                "commission": 0.0, "lots": 0.01, "user_id": uid})
        cert = await qualify_account(
            db, await db.accounts.find_one({"_id": acc_id}))
        assert cert["checks"]["netting_hedging"]["value"] == "netting"
        assert cert["checks"]["commission_model"]["value"] == "spread-only"
        assert cert["checks"]["partial_fills"]["value"] is False
        assert cert["tier"] == "PROVISIONAL"  # no intel score yet
        stored = await db.broker_certifications.find_one(
            {"account_id": str(acc_id)})
        assert stored and stored["tier"] == "PROVISIONAL"
        await db.accounts.delete_one({"_id": acc_id})
        await db.broker_deals.delete_many({"user_id": uid})
        await db.broker_certifications.delete_many(
            {"account_id": str(acc_id)})
    _run(go())


def test_soak_sampler_rss_probe():
    from background_loops import _rss_mb
    rss = _rss_mb()
    assert rss is None or rss > 10
