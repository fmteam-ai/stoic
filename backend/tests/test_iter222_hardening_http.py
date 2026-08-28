"""iter-222 / v62.7 — Risk Truth hardening invariants (HTTP + direct guard).

The three critical invariants:
  I1 LIVE PAMM + Risk Truth unavailable:
       BUY            → REJECT (risk_unknown)
       CLOSE position → ALLOW
       REDUCE         → ALLOW
       INCREASE       → REJECT
  I2 Risk Truth collection throwing (Mongo timeout) →
       RISK_UNKNOWN, not 500, and NEVER AUTHORIZED (new risk, LIVE).
  I3 Nitro with fresh spread/position truth but STALE latency evidence →
       INELIGIBLE (hard_floor: latency_quality), never execution on old
       quality data.
Plus: stale NAV evidence → risk_unknown listing drawdown_pct.
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.http

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
STEP_UP_BYPASS = os.environ.get("STEP_UP_BYPASS_TOKEN", "")

ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def mongo():
    c = MongoClient(MONGO_URL)
    yield c[DB_NAME]
    c.close()


def _csrf(session):
    tok = session.cookies.get("csrf_token")
    assert tok, "csrf_token cookie missing"
    return {"X-CSRF-Token": tok}


def _headers(session, step_up=False):
    h = _csrf(session)
    if step_up and STEP_UP_BYPASS:
        h["X-Step-Up-Bypass"] = STEP_UP_BYPASS
    return h


def _allow_http_cookies(s):
    # CI targets plain http://127.0.0.1 — requests refuses to SEND cookies
    # flagged Secure over http. The flag is a browser transport concern;
    # strip it client-side right before each request is prepared (CI only).
    orig = s.prepare_request

    def prep(req):
        for c in s.cookies:
            c.secure = False
        return orig(req)

    s.prepare_request = prep


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    if BASE_URL.startswith("http://"):
        _allow_http_cookies(s)
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    assert r.status_code == 200, r.text
    yield s


@pytest.fixture(scope="module")
def admin_id(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/auth/me", timeout=15)
    return r.json()["id"]


def _create_program(admin_session, suffix):
    name = f"TEST_iter222_{suffix}_{uuid.uuid4().hex[:6]}"
    r = admin_session.post(f"{BASE_URL}/api/pamm/programs",
                           json={"name": name},
                           headers=_csrf(admin_session), timeout=20)
    assert r.status_code in (200, 201), r.text
    pid = r.json()["program_id"]
    c = MongoClient(MONGO_URL)
    c[DB_NAME].pamm_programs.update_one(
        {"program_id": pid},
        {"$set": {"risk_limits.news_filter.enabled": False}})
    c.close()
    return pid


def _insert_account(mongo, admin_id, broker_env="LIVE", balance=10000.0):
    acc_id = ObjectId()
    mongo.accounts.insert_one({
        "_id": acc_id, "user_id": admin_id,
        "broker": "TestBroker222", "server": "TestServer222-A",
        "mode": "paper", "broker_environment": broker_env,
        "balance": balance, "equity": balance,
        "bridge_token": f"bt_222_{uuid.uuid4().hex}",
        "account_number": f"iter222_{uuid.uuid4().hex[:6]}"})
    return str(acc_id)


def _assign(admin_session, pid, strategy_id="sniper",
            risk_profile_id="controlled"):
    r = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
        json={"mode": "SINGLE", "strategy_id": strategy_id,
              "risk_profile_id": risk_profile_id},
        headers=_csrf(admin_session), timeout=15)
    assert r.status_code == 200, r.text


def _validate(admin_session, pid):
    r = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/strategy/validate",
        headers=_csrf(admin_session), timeout=20)
    assert r.status_code == 200, r.text


def _activate(admin_session, pid):
    """Direct activation — the HTTP route demands full LIVE certification,
    which is out of scope here (the guard's cert check is disabled via the
    feature flag in _guard_call; these tests target RISK-TRUTH behavior)."""
    c = MongoClient(MONGO_URL)
    c[DB_NAME].pamm_strategy_assignments.update_one(
        {"pamm_program_id": pid,
         "status": {"$in": ["ASSIGNED", "SUSPENDED", "ACTIVE"]}},
        {"$set": {"status": "ACTIVE", "enabled": True}})
    c.close()


def _seed_full_risk_truth(mongo, pid, acc_id):
    now = datetime.now(timezone.utc).isoformat()
    mongo.accounts.update_one(
        {"_id": ObjectId(acc_id)},
        {"$set": {"current_spreads": {"EURUSD": 0.8},
                  "spreads_updated_at": now}})
    mongo.pamm_programs.update_one(
        {"program_id": pid},
        {"$set": {"position_truth": {"status": "in_sync", "at": now},
                  "last_nav": {"nav": 10000.0, "at": now}}})
    mongo.pamm_nav_snapshots.insert_one(
        {"program_id": pid, "nav": 10000.0, "at": now})


def _cleanup(mongo, pid, acc_id):
    mongo.pamm_programs.delete_one({"program_id": pid})
    mongo.pamm_strategy_assignments.delete_many({"pamm_program_id": pid})
    mongo.pamm_nav_snapshots.delete_many({"program_id": pid})
    mongo.pamm_risk_decisions.delete_many({"program_id": pid})
    mongo.pamm_events.delete_many({"payload.program_id": pid})
    mongo.trades.delete_many({"account_id": acc_id})
    mongo.accounts.delete_one({"_id": ObjectId(acc_id)})


def _run(coro_fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


async def _guard_call(acc_id, signal, no_cert=True):
    """Direct in-process guard call. no_cert disables the certification
    requirement so LIVE risk-truth checks are reachable without walking
    a full certification campaign."""
    import modules.pamm.strategy_assignment as sa
    from modules.pamm.strategy_guard import (
        authorize_pamm_strategy_execution, resolve_program)
    orig = sa.feature_flags
    if no_cert:
        sa.feature_flags = lambda: {**orig(),
                                    "PAMM_REQUIRE_CERTIFICATION": False}
    mc = AsyncIOMotorClient(MONGO_URL)
    db = mc[DB_NAME]
    try:
        prog = await resolve_program(db, acc_id, signal)
        acc = await db.accounts.find_one({"_id": ObjectId(acc_id)})
        return await authorize_pamm_strategy_execution(
            db, prog, acc, signal)
    finally:
        sa.feature_flags = orig
        mc.close()


@pytest.fixture(scope="class")
def live_setup(request, admin_session, admin_id):
    c = MongoClient(MONGO_URL)
    mongo = c[DB_NAME]
    pid = _create_program(admin_session, "inv")
    acc_id = _insert_account(mongo, admin_id, "LIVE")
    mongo.pamm_programs.update_one(
        {"program_id": pid},
        {"$set": {"master_account_id": acc_id}})
    _assign(admin_session, pid, "sniper", "controlled")
    _validate(admin_session, pid)
    _activate(admin_session, pid)
    # a real open position exists — CLOSE/REDUCE labels must match reality
    mongo.trades.insert_one({
        "trade_id": f"iter222_open_{uuid.uuid4().hex[:8]}",
        "user_id": admin_id, "account_id": acc_id,
        "symbol": "EURUSD", "action": "BUY", "lot_size": 0.05,
        "status": "open",
        "opened_at": datetime.now(timezone.utc).isoformat()})
    yield {"pid": pid, "acc_id": acc_id, "mongo": mongo}
    _cleanup(mongo, pid, acc_id)
    c.close()


# ═════ I1 · Fail-safe asymmetry when Risk Truth is UNAVAILABLE ════════════

class TestI1FailSafeAsymmetry:
    """No spreads / no NAV / no position-truth seeded — Risk Truth is
    UNAVAILABLE on a LIVE governed program."""

    def test_buy_rejected_risk_unknown(self, live_setup):
        out = _run(lambda: _guard_call(live_setup["acc_id"], {
            "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
            "strategy_id": "sniper", "risk_pct": 0.1}))
        assert out["authorized"] is False, out
        assert out["reason"] == "risk_unknown", out
        det = out["checks"][-1]["detail"]
        assert det["state"] == "RISK_UNKNOWN"
        assert "spread_pips" in det["missing_required_evidence"]

    def test_close_allowed(self, live_setup):
        out = _run(lambda: _guard_call(live_setup["acc_id"], {
            "symbol": "EURUSD", "action": "CLOSE", "lot_size": 0.01,
            "strategy_id": "sniper"}))
        assert out["authorized"] is True, out
        assert any(c["key"] == "risk_reducing" for c in out["checks"])

    def test_reduce_allowed(self, live_setup):
        out = _run(lambda: _guard_call(live_setup["acc_id"], {
            "symbol": "EURUSD", "action": "SELL", "reduce_only": True,
            "lot_size": 0.01, "strategy_id": "sniper"}))
        assert out["authorized"] is True, out

    def test_increase_rejected(self, live_setup):
        out = _run(lambda: _guard_call(live_setup["acc_id"], {
            "symbol": "EURUSD", "action": "SELL", "lot_size": 0.02,
            "strategy_id": "sniper", "risk_pct": 0.2}))
        assert out["authorized"] is False, out
        assert out["reason"] == "risk_unknown", out

    def test_fraudulent_risk_reducing_label_rejected(self, live_setup):
        """SEC v62.7 — a 'CLOSE' on a symbol with NO open position is a
        guard-bypass attempt, not a safety exit."""
        out = _run(lambda: _guard_call(live_setup["acc_id"], {
            "symbol": "GBPJPY", "action": "CLOSE",
            "strategy_id": "sniper"}))
        assert out["authorized"] is False, out
        assert out["reason"] == "risk_reducing_label_invalid", out

    def test_close_allowed_even_when_program_paused(self, live_setup):
        mongo = live_setup["mongo"]
        mongo.pamm_programs.update_one(
            {"program_id": live_setup["pid"]},
            {"$set": {"op_state": "new_trades_paused"}})
        try:
            out = _run(lambda: _guard_call(live_setup["acc_id"], {
                "symbol": "EURUSD", "action": "CLOSE",
                "strategy_id": "sniper"}))
            assert out["authorized"] is True, out
        finally:
            mongo.pamm_programs.update_one(
                {"program_id": live_setup["pid"]},
                {"$unset": {"op_state": ""}})


# ═════ Stale NAV is not evidence ══════════════════════════════════════════

class TestStaleNavRejects:
    def test_stale_nav_yields_risk_unknown_on_drawdown(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "nav")
        acc_id = _insert_account(mongo, admin_id, "LIVE")
        mongo.pamm_programs.update_one(
            {"program_id": pid}, {"$set": {"master_account_id": acc_id}})
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            _seed_full_risk_truth(mongo, pid, acc_id)
            # age the NAV evidence beyond NAV_FRESHNESS_S (900s)
            old = (datetime.now(timezone.utc)
                   - timedelta(hours=2)).isoformat()
            mongo.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"last_nav.at": old}})
            mongo.pamm_nav_snapshots.update_many(
                {"program_id": pid}, {"$set": {"at": old}})
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper", "risk_pct": 0.1}))
            assert out["authorized"] is False, out
            assert out["reason"] == "risk_unknown", out
            det = out["checks"][-1]["detail"]
            assert det["missing_required_evidence"] == ["drawdown_pct"], det
        finally:
            _cleanup(mongo, pid, acc_id)


# ═════ I2 · Mongo timeout during collection ⇒ RISK_UNKNOWN ════════════════

class TestI2CollectionFailure:
    def test_timeout_is_risk_unknown_never_authorized(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "boom")
        acc_id = _insert_account(mongo, admin_id, "LIVE")
        mongo.pamm_programs.update_one(
            {"program_id": pid}, {"$set": {"master_account_id": acc_id}})
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            _seed_full_risk_truth(mongo, pid, acc_id)

            async def call():
                import modules.pamm.strategy_assignment as sa
                from modules.pamm import strategy_guard as g
                orig_flags = sa.feature_flags
                orig_t = g._telemetry

                async def boom(*a, **k):
                    raise RuntimeError("simulated mongo timeout")

                sa.feature_flags = lambda: {
                    **orig_flags(), "PAMM_REQUIRE_CERTIFICATION": False}
                g._telemetry = boom
                mc = AsyncIOMotorClient(MONGO_URL)
                db = mc[DB_NAME]
                try:
                    prog = await g.resolve_program(db, acc_id, {})
                    acc = await db.accounts.find_one(
                        {"_id": ObjectId(acc_id)})
                    return await g.authorize_pamm_strategy_execution(
                        db, prog, acc,
                        {"symbol": "EURUSD", "action": "BUY",
                         "lot_size": 0.01, "strategy_id": "sniper",
                         "risk_pct": 0.1})
                finally:
                    g._telemetry = orig_t
                    sa.feature_flags = orig_flags
                    mc.close()

            out = _run(call)  # must not raise (no 500)
            assert out["authorized"] is False, out
            assert out["reason"] == "risk_unknown", out
        finally:
            _cleanup(mongo, pid, acc_id)


# ═════ I3 · Nitro stale latency evidence ⇒ INELIGIBLE ═════════════════════

class TestI3NitroLatencyFreshness:
    def _seed_latency_trades(self, mongo, user_id, acc_id, opened_at):
        ids = []
        for _ in range(3):
            r = mongo.trades.insert_one({
                "trade_id": f"iter222_lat_{uuid.uuid4().hex[:8]}",
                "user_id": user_id, "account_id": acc_id,
                "symbol": "EURUSD", "status": "closed",
                "opened_at": opened_at,
                "latency_trace": {"t0_ms": 0, "t9_ms": 350}})
            ids.append(r.inserted_id)
        return ids

    def test_fresh_vs_stale_latency_evidence(self, mongo, admin_id):
        test_user = f"iter222_u_{uuid.uuid4().hex[:8]}"
        acc_id = _insert_account(mongo, test_user, "LIVE")
        now_iso = datetime.now(timezone.utc).isoformat()
        mongo.accounts.update_one(
            {"_id": ObjectId(acc_id)},
            {"$set": {"current_spreads": {"EURUSD": 0.8},
                      "spreads_updated_at": now_iso}})
        try:
            async def elig():
                from strategies.execution_eligibility import \
                    execution_eligibility
                mc = AsyncIOMotorClient(MONGO_URL)
                db = mc[DB_NAME]
                try:
                    acc = await db.accounts.find_one(
                        {"_id": ObjectId(acc_id)})
                    return await execution_eligibility(
                        db, "nitro_scalper", test_user, acc)
                finally:
                    mc.close()

            # FRESH latency samples → latency_quality is evidence-based
            self._seed_latency_trades(mongo, test_user, acc_id, now_iso)
            fresh_out = _run(elig)
            assert fresh_out["components"]["latency_quality"] > 40.0, \
                fresh_out["components"]

            # Age ALL latency samples beyond the freshness window
            old = (datetime.now(timezone.utc)
                   - timedelta(hours=2)).isoformat()
            mongo.trades.update_many(
                {"user_id": test_user}, {"$set": {"opened_at": old}})
            stale_out = _run(elig)
            assert stale_out["status"] == "INELIGIBLE", stale_out
            assert "latency_quality" in stale_out["reason"], stale_out
            assert stale_out["components"][
                "latency_evidence_age_s"] > 1800
        finally:
            mongo.trades.delete_many({"user_id": test_user})
            mongo.accounts.delete_one({"_id": ObjectId(acc_id)})


# ═════ P0.2 · Application-path proof (submit_intent + HTTP close) ═════════

@pytest.fixture(scope="class")
def app_setup(admin_session, admin_id):
    c = MongoClient(MONGO_URL)
    mongo = c[DB_NAME]
    pid = _create_program(admin_session, "apppath")
    acc_id = _insert_account(mongo, admin_id, "LIVE")
    mongo.pamm_programs.update_one(
        {"program_id": pid},
        {"$set": {"master_account_id": acc_id}})
    _assign(admin_session, pid, "sniper", "controlled")
    _validate(admin_session, pid)
    _activate(admin_session, pid)
    tr = mongo.trades.insert_one({
        "trade_id": f"iter222_app_{uuid.uuid4().hex[:8]}",
        "user_id": admin_id, "account_id": acc_id,
        "symbol": "EURUSD", "action": "BUY", "lot_size": 0.05,
        "status": "open",
        "opened_at": datetime.now(timezone.utc).isoformat()})
    yield {"pid": pid, "acc_id": acc_id, "mongo": mongo,
           "open_trade_id": str(tr.inserted_id)}
    _cleanup(mongo, pid, acc_id)
    c.close()


class TestApplicationPathRiskUnknown:
    """Risk Unknown proven through the REAL execution pipeline
    (ExecutionIntent → PAMM guard → authority), not a direct guard call,
    and CLOSE through the actual HTTP route."""

    def test_buy_blocked_via_submit_intent_pipeline(self, app_setup):
        acc_id = app_setup["acc_id"]

        async def call():
            import os as _os
            from execution import PaperEngine
            from execution_authority import submit_intent
            _os.environ["PAMM_REQUIRE_CERTIFICATION"] = "false"
            mc = AsyncIOMotorClient(MONGO_URL)
            db = mc[DB_NAME]
            try:
                import database
                orig = database.get_db
                database.get_db = lambda: db
                try:
                    acc = await db.accounts.find_one(
                        {"_id": ObjectId(acc_id)})
                    return await submit_intent(
                        user_id=str(acc["user_id"]), account=acc,
                        signal={"symbol": "EURUSD", "action": "BUY",
                                "lot_size": 0.01, "entry_price": 1.08,
                                "stop_loss": 1.07, "take_profit": 1.1,
                                "strategy_id": "sniper", "risk_pct": 0.1,
                                "signal_id": uuid.uuid4().hex},
                        engine=PaperEngine())
                finally:
                    database.get_db = orig
                    _os.environ.pop("PAMM_REQUIRE_CERTIFICATION", None)
            finally:
                mc.close()

        out = _run(call)
        assert out.get("blocked") == "pamm_strategy_guard", out
        assert out.get("reason") == "risk_unknown", out
        # the intent itself was finalized REJECTED (auditable)
        intent = app_setup["mongo"].execution_intents.find_one(
            {"intent_id": out["intent_id"]})
        assert intent and intent["status"] == "rejected", intent

    def test_close_allowed_via_http_route(self, app_setup, admin_session):
        r = admin_session.post(
            f"{BASE_URL}/api/trades/{app_setup['open_trade_id']}/close",
            headers=_csrf(admin_session), timeout=15)
        assert r.status_code == 200, r.text
        assert r.json().get("ok") is True
        doc = app_setup["mongo"].trades.find_one(
            {"_id": ObjectId(app_setup["open_trade_id"])})
        assert doc["close_requested"] is True
        assert doc["status"] == "pending"

    def test_http_execute_route_cannot_bypass_guard(self, app_setup,
                                                    admin_session,
                                                    admin_id):
        """The user-facing execute route on a governed master is ALSO
        stopped by the guard (explicit provenance is required)."""
        mongo = app_setup["mongo"]
        sig = mongo.signals.insert_one({
            "user_id": admin_id, "symbol": "EURUSD", "action": "BUY",
            "lot_size": 0.01, "entry_price": 1.08, "stop_loss": 1.07,
            "take_profit": 1.1, "confidence": 80,
            "created_at": datetime.now(timezone.utc).isoformat()})
        try:
            r = admin_session.post(
                f"{BASE_URL}/api/trades/execute/{sig.inserted_id}",
                json={"account_id": app_setup["acc_id"]},
                headers=_csrf(admin_session), timeout=20)
            if r.status_code == 429:
                pytest.skip("order rate limit window — covered by "
                            "submit_intent pipeline test above")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body.get("blocked") == "pamm_strategy_guard", body
        finally:
            mongo.signals.delete_one({"_id": sig.inserted_id})


# ═════ Snapshot hash present on persisted decisions ═══════════════════════

class TestSnapshotHashPersisted:
    def test_decision_snapshot_is_hashed_with_provenance(self, live_setup):
        mongo = live_setup["mongo"]
        mongo.pamm_risk_decisions.delete_many(
            {"program_id": live_setup["pid"]})
        _run(lambda: _guard_call(live_setup["acc_id"], {
            "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
            "strategy_id": "sniper", "risk_pct": 0.1}))
        snap = mongo.pamm_risk_decisions.find_one(
            {"program_id": live_setup["pid"]})
        assert snap, "decision snapshot missing"
        assert len(snap.get("hash") or "") == 64
        prov = snap.get("provenance") or {}
        assert prov.get("guard_policy_version") == "v62.8"
        assert prov.get("execution_policy_version")
        # hash verifies against content
        from modules.pamm.strategy_guard import _snapshot_hash
        body = {k: v for k, v in snap.items() if k not in ("_id", "hash")}
        assert _snapshot_hash(body) == snap["hash"]
