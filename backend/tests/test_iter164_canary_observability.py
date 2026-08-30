"""iter-164 — canary campaign records more than the block rate.

Covers observability_snapshot(): position truth, connection state,
latency vs fleet, slippage vs fleet, infrastructure alerts, and its
wiring into canary evaluate() / status().
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import release_canary as rc  # noqa: E402

pytestmark = pytest.mark.integration


def _db():
    from motor.motor_asyncio import AsyncIOMotorClient
    return AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_snapshot_contains_all_dimensions():
    async def go():
        db = _db()
        acc = await db.accounts.find_one({})
        assert acc, "need at least one account in preview DB"
        return await rc.observability_snapshot(
            db, {"account_id": str(acc["_id"])})
    obs = _run(go())
    names = {o["dimension"] for o in obs}
    assert {"latency_ms", "slippage_abs_pips"} <= names, names
    assert "position_truth" in names and "connection" in names, names
    lat = next(o for o in obs if o["dimension"] == "latency_ms")
    assert set(lat["canary"]) == {"samples", "avg"}
    assert set(lat["fleet"]) == {"samples", "avg"}


def test_snapshot_never_raises_for_unknown_account():
    obs = _run(rc.observability_snapshot(
        _db(), {"account_id": f"ghost-{uuid.uuid4().hex}"}))
    assert isinstance(obs, list)
    names = {o["dimension"] for o in obs}
    assert {"latency_ms", "slippage_abs_pips"} <= names


def test_infrastructure_dimension_excludes_synthetic_alerts():
    async def go():
        db = _db()
        tag = f"iter164_{uuid.uuid4().hex[:8]}"
        await db.ops_alerts.insert_one({
            "alert_id": tag, "severity": "critical", "acked_at": None,
            "synthetic": True,
            "created_at": datetime.now(timezone.utc).isoformat()})
        try:
            real = await db.ops_alerts.count_documents(
                {"acked_at": None, "severity": "critical",
                 "synthetic": {"$ne": True}})
            obs = await rc.observability_snapshot(
                db, {"account_id": "none"})
            infra = next(o for o in obs if o["dimension"] == "infrastructure")
            assert infra["open_critical_alerts"] == real
            assert infra["healthy"] == (real == 0)
        finally:
            await db.ops_alerts.delete_one({"alert_id": tag})
    _run(go())


def test_status_and_evaluate_expose_observability():
    async def go():
        db = _db()
        st = await db.platform_state.find_one({"_id": rc.STATE_ID})
        started = bool(st and st.get("active"))
        status = await rc.status(db)
        if started:
            assert "observability" in status, status.keys()
            ev = await rc.evaluate(db)
            if ev.get("evaluated"):
                assert "observability" in ev
        else:
            assert status.get("active") is not True
    _run(go())
