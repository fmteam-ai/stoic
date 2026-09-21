"""iter-201 — release review corrections.

P0-1  enablement: EVERY execution-selection site uses trading_enabled == True
      (missing = OFF) — asserted statically + behaviourally on real Mongo.
P1-4  GET /bot/health-score is read-only; repairs live in health_repairs
      with an immutable ledger.
P1-5  hard-cap evaluation failure → health_truth_unavailable cap 25.
P2-1  soak UNKNOWN threshold: LIVE lane = 0.0, research = 0.2.
P1-3  forecast telemetry is labelled advisory/fail-open and records
      whether the last decision consumed a forecast.
"""
import asyncio
import inspect
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
TAG = uuid.uuid4().hex[:8]


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _db():
    return AsyncIOMotorClient(MONGO_URL)[DB_NAME]


# ───────────── P0-1 ─────────────────────────────────────────────────────

def test_no_execution_site_treats_missing_enablement_as_on():
    import alerting
    import bot_runner
    import state_contract
    from routes import account_routes, bot_routes, enterprise_routes, metrics_routes
    for mod in (bot_runner, alerting, bot_routes, state_contract,
                account_routes, enterprise_routes, metrics_routes):
        src = inspect.getsource(mod)
        assert not re.search(r'"trading_enabled":\s*\{"\$ne":\s*False\}', src), \
            f"{mod.__name__}: $ne False selects missing enablement as ON"
        assert 'get("trading_enabled") is False' not in src, \
            f"{mod.__name__}: `is False` treats missing enablement as ON"
        assert 'get("trading_enabled") is not False' not in src, mod.__name__


def test_worker_selection_excludes_legacy_record_without_flag():
    from bot_runner import _connected_accounts

    async def scenario():
        db = _db()
        uid = f"u_en_{TAG}"
        now = datetime.now(timezone.utc).isoformat()
        base = {"user_id": uid, "status": "connected", "last_heartbeat": now,
                "mode": "live", "server": "X-Real"}
        ids = [ObjectId(), ObjectId(), ObjectId()]
        await db.accounts.insert_many([
            {**base, "_id": ids[0], "label": "on", "trading_enabled": True,
             "bridge_token": f"t{TAG}a"},
            {**base, "_id": ids[1], "label": "off", "trading_enabled": False,
             "bridge_token": f"t{TAG}b"},
            {**base, "_id": ids[2], "label": "legacy-missing",
             "bridge_token": f"t{TAG}c"},
        ])
        try:
            rows = await _connected_accounts(db, uid)
            labels = sorted(a["label"] for a in rows)
            assert labels == ["on"], labels
            from state_contract import contract
            sc = await contract(db, uid)
            assert sc["totals"]["accounts_enabled"] == 1   # UI == worker
        finally:
            await db.accounts.delete_many({"user_id": uid})
    _run(scenario())


# ───────────── P1-4 ─────────────────────────────────────────────────────

def test_health_score_get_has_no_writes():
    from routes import bot_routes
    src = inspect.getsource(bot_routes.bot_health_score)
    for verb in ("update_many", "update_one", "insert_one", "delete_many",
                 "delete_one", "find_one_and_update"):
        assert verb not in src, f"GET /bot/health-score still calls {verb}"


def test_health_repairs_ledger_and_idempotency():
    from health_repairs import run_health_repairs

    async def scenario():
        db = _db()
        uid = f"u_rep_{TAG}"
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=2)).isoformat()
        acc = ObjectId()
        await db.accounts.insert_one(
            {"_id": acc, "user_id": uid, "label": "silent", "status": "connected",
             "last_heartbeat": old, "trading_enabled": True,
             "bridge_token": f"t{TAG}r"})
        await db.trades.insert_one(
            {"user_id": uid, "account_id": str(acc), "status": "open",
             "symbol": "XAUUSD", "label": f"TEST_{TAG}",
             "pending_modification": {"requested_at": old, "type": "sl"}})
        await db.trades.insert_one(
            {"user_id": uid, "account_id": str(acc), "status": "closed",
             "exit_price": None, "closed_at": (now - timedelta(hours=30)).isoformat(),
             "symbol": "XAUUSD", "label": f"TEST_{TAG}"})
        try:
            r1 = await run_health_repairs(db, uid)
            assert r1["dormant"] == 1 and r1["pending_expired"] == 1 \
                and r1["ghost_old"] == 1
            a = await db.accounts.find_one({"_id": acc})
            assert a["dormant"] is True and a["status"] == "disconnected"
            ledger = [x async for x in db.repair_ledger.find(
                {"correlation_id": r1["correlation_id"]})]
            kinds = sorted(x["kind"] for x in ledger)
            assert kinds == ["account_dormant", "ghost_ack_old",
                             "pending_modification_expired"]
            assert all(x["affected_ids"] for x in ledger)
            r2 = await run_health_repairs(db, uid)      # idempotent
            assert (r2["dormant"], r2["pending_expired"], r2["ghost_old"]) == (0, 0, 0)
        finally:
            await db.accounts.delete_many({"user_id": uid})
            await db.trades.delete_many({"user_id": uid})
            await db.repair_ledger.delete_many({"user_id": uid})
    _run(scenario())


# ───────────── P1-5 ─────────────────────────────────────────────────────

def test_hard_cap_evaluation_fails_closed():
    from routes import bot_routes
    src = inspect.getsource(bot_routes.bot_health_score)
    assert src.count('"health_truth_unavailable"') >= 2
    assert "except Exception:  # noqa: BLE001 — caps degrade gracefully" not in src


# ───────────── P2-1 ─────────────────────────────────────────────────────

def test_soak_unknown_thresholds_are_lane_specific():
    from soak_campaign import (UNKNOWN_RATE_MAX_LIVE, UNKNOWN_RATE_MAX_RESEARCH,
                               unknown_rate_threshold)
    assert UNKNOWN_RATE_MAX_LIVE == 0.0
    assert UNKNOWN_RATE_MAX_RESEARCH == 0.2
    assert unknown_rate_threshold("LIVE") == 0.0
    assert unknown_rate_threshold("DEMO") == 0.2
    assert unknown_rate_threshold(None) == 0.2


# ───────────── P1-3 ─────────────────────────────────────────────────────

def test_forecast_status_is_labelled_advisory_and_tracks_decisions():
    import forecast_agent as fa
    fa.record_decision(None)
    fa.record_decision({"q50": 1})
    st = fa.runtime_status()
    assert st["advisory"] is True and st["fail_open"] is True
    assert "FAIL-OPEN" in st["notice"]
    assert st["last_decision_consumed"] is True
    assert st["decisions_with_forecast"] >= 1
    assert st["decisions_without_forecast"] >= 1
