"""iter-95 — Phase 8/9: signed attestation, broker certification, evidence board."""
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

from differentiation import (canonical_hash, certify, feature_evidence,
                             perf_attestation, verify_attestation)


@pytest.fixture()
def db():
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


UID = f"iter95-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------- canonical hash
def test_canonical_hash_deterministic_and_order_independent():
    a = {"net": 12.5, "accounts": [{"x": 1}], "when": "2026-06-01"}
    b = {"when": "2026-06-01", "accounts": [{"x": 1}], "net": 12.5}
    assert canonical_hash(a) == canonical_hash(b)
    assert len(canonical_hash(a)) == 64


def test_canonical_hash_change_sensitive():
    a = {"net": 12.5}
    assert canonical_hash(a) != canonical_hash({"net": 12.51})


# ------------------------------------------------------------ attestation
def test_attestation_roundtrip():
    payload = {"overall": {"net_pnl": 431.2}, "max_drawdown": 88.0}
    att = perf_attestation(payload)
    assert att["key_id"] == "perf-ed25519-v1"  # review P1-4: Ed25519
    assert verify_attestation(att["payload_hash"], att["signature"])


def test_attestation_tamper_detected():
    att = perf_attestation({"overall": {"net_pnl": 431.2}})
    tampered_hash = canonical_hash({"overall": {"net_pnl": 9999.0}})
    assert not verify_attestation(tampered_hash, att["signature"])
    assert not verify_attestation(att["payload_hash"], "0" * 64)


# ---------------------------------------------------------- certification
def test_certify_tiers():
    assert certify(None, False, 50)["tier"] == "PROVISIONAL"
    assert certify(90, True, 50)["tier"] == "PROVISIONAL"
    assert certify(90, False, 5)["tier"] == "PROVISIONAL"
    assert certify(85, False, 20)["tier"] == "CERTIFIED"
    assert certify(80, False, 10)["tier"] == "CERTIFIED"
    assert certify(60, False, 20)["tier"] == "ACCEPTABLE"
    assert certify(40, False, 20)["tier"] == "DEGRADED"


# --------------------------------------------------------- evidence board
async def _cleanup(db, acc_id):
    await db.accounts.delete_many({"user_id": UID})
    await db.execution_timing_stats.delete_many({"account_id": acc_id})
    for c in ("trade_events", "trades", "strategy_allocations",
              "bot_configs", "learning_runs"):
        await getattr(db, c).delete_many({"user_id": UID})


def test_feature_evidence_empty_user(db):
    async def go():
        out = await feature_evidence(db, f"nobody-{uuid.uuid4().hex[:6]}")
        assert len(out["features"]) == 6
        assert out["summary"]["proven"] == 0
        assert all(f["verdict"] == "experimental" for f in out["features"])
    _run(go())


def test_feature_evidence_proven_paths(db):
    acc_id = f"acc-{UID}"

    async def go():
        await _cleanup(db, acc_id)
        now = datetime.now(timezone.utc)
        from bson import ObjectId
        oid = ObjectId()
        await db.accounts.insert_one(
            {"_id": oid, "user_id": UID, "label": "T", "status": "active",
             "bridge_token": f"tok-{UID}"})
        # execution timing: 25 delays, 60% improved, net spread saved
        await db.execution_timing_stats.insert_many([
            {"account_id": str(oid), "spread_before": 3.0,
             "spread_after": 2.0 if i % 5 < 3 else 3.5,
             "improved": i % 5 < 3, "at": now - timedelta(hours=i)}
            for i in range(25)])
        # adaptive exits: 20 protective actions
        await db.trade_events.insert_many([
            {"user_id": UID, "event_type": "StopTightened",
             "source": "adaptive_exits", "at": now} for _ in range(20)])
        # regime gating: 45 stamped trades
        await db.trades.insert_many([
            {"user_id": UID, "market_regime": {"key": "LOW_VOL_TREND"},
             "status": "closed"} for _ in range(45)])
        # dynamic allocation: weights shifted vs static split
        from risk_budget import DEFAULT_ALLOCATIONS
        weights = dict(DEFAULT_ALLOCATIONS)
        ks = list(weights)
        weights[ks[0]] = round(weights[ks[0]] + 0.10, 4)
        weights[ks[1]] = round(weights[ks[1]] - 0.10, 4)
        await db.strategy_allocations.insert_one(
            {"user_id": UID, "account_id": None, "weights": weights,
             "n_trades": 60, "at": now})
        # risk layers: one tripped breaker
        await db.bot_configs.insert_one(
            {"user_id": UID, "active": False,
             "tripped_at": now.isoformat()})
        # learning pipeline: one rejected retrain
        await db.learning_runs.insert_one(
            {"user_id": UID, "at": now, "frozen": False,
             "stages": {"ml_ensemble": {"status": "rejected"}}})

        out = await feature_evidence(db, UID, days=30)
        by = {f["feature"]: f for f in out["features"]}
        assert by["execution_timing"]["verdict"] == "proven"
        assert by["adaptive_exits"]["verdict"] == "proven"
        assert by["regime_gating"]["verdict"] == "proven"
        assert by["dynamic_allocation"]["verdict"] == "proven"
        assert by["risk_layers"]["verdict"] == "proven"
        assert by["learning_pipeline"]["verdict"] == "proven"
        assert out["summary"]["proven"] == 6
        await _cleanup(db, str(oid))
    _run(go())


def test_feature_evidence_review_on_negative_timing(db):
    acc_id = None

    async def go():
        nonlocal acc_id
        uid = f"{UID}-neg"
        now = datetime.now(timezone.utc)
        from bson import ObjectId
        oid = ObjectId()
        acc_id = str(oid)
        await db.accounts.insert_one(
            {"_id": oid, "user_id": uid, "label": "N", "status": "active",
             "bridge_token": f"tok-{uid}"})
        # 25 delays that all WORSENED the fill → review
        await db.execution_timing_stats.insert_many([
            {"account_id": acc_id, "spread_before": 2.0, "spread_after": 3.5,
             "improved": False, "at": now} for _ in range(25)])
        out = await feature_evidence(db, uid, days=30)
        by = {f["feature"]: f for f in out["features"]}
        assert by["execution_timing"]["verdict"] == "review"
        assert out["summary"]["review"] == 1
        await db.accounts.delete_many({"user_id": uid})
        await db.execution_timing_stats.delete_many({"account_id": acc_id})
    _run(go())


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
