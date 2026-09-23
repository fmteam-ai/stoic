from live_target import ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-221 / v62.6 — PAMM Risk Truth & Production Gate HTTP + direct-guard tests.

Covers the previously untested v62.6 semantics:
  R1 LIVE governed program w/ ACTIVE assignment but NO risk evidence
     (no current_spreads, no NAV snapshots)  ->  reason='risk_unknown'
     with detail.missing_required_evidence listing the gaps.
  R2 LIVE full evidence, position_truth MISSING on program doc
     ->  reject 'position_truth_missing'.
  R3 LIVE full evidence, position_truth.at OLDER than sniper freshness
     (sniper latency_sensitivity=HIGH -> 600s ; seed 20 min old)
     ->  reject 'position_truth_stale'.
  R4 LIVE full FRESH evidence -> authorized ; a doc appears in
     pamm_risk_decisions with authorized=True, envelope/telemetry/checks
     populated; response.context.risk_snapshot_id returned.
  R5 Weekly loss cap: DEMO, seed closed trades THIS WEEK with net loss
     >= max_weekly_loss_pct of balance -> 'weekly_loss_cap_reached'.
  R6 Drawdown cap: seed pamm_nav_snapshots peak + low current nav
     -> 'drawdown_cap_reached'.
  R7 Spread cap (fresh) -> 'spread_cap_exceeded'.
     Spread telemetry STALE on DEMO -> spread violation NOT fabricated
     (telemetry.spread_pips is None, other checks pass).
  R8 Factor exposure: 1.9 lots GBPUSD open + new EURUSD 0.2 lot
     (profile max_factor_exposure=2.0) -> 'factor_exposure_exceeded' (USD).
  R9 Pre-trade slippage: seed trades with slippage_checked=True and
     median slippage_pips above profile max -> 'expected_slippage_exceeded'.
  R10 Rejected guard decisions ALSO persist a snapshot doc to
     pamm_risk_decisions with authorized=False and reason set.
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

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
STEP_UP_BYPASS = os.environ.get("STEP_UP_BYPASS_TOKEN", "")

ADMIN_EMAIL = "admin@stoicaibot.com"
pass  # ADMIN_PASSWORD comes from live_target
# ─────────── fixtures & helpers ──────────────────────────────────────────

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
    name = f"TEST_iter221_{suffix}_{uuid.uuid4().hex[:6]}"
    r = admin_session.post(f"{BASE_URL}/api/pamm/programs",
                           json={"name": name},
                           headers=_csrf(admin_session), timeout=20)
    assert r.status_code in (200, 201), r.text
    pid = r.json()["program_id"]
    # tests must not depend on the live news calendar — disable blackout
    c = MongoClient(MONGO_URL)
    c[DB_NAME].pamm_programs.update_one(
        {"program_id": pid},
        {"$set": {"risk_limits.news_filter.enabled": False}})
    c.close()
    return pid


def _insert_paper_account(mongo, admin_id, broker_env="DEMO",
                         broker="TestBroker221",
                         server="TestServer221-A", balance=10000.0):
    acc_id = ObjectId()
    mongo.accounts.insert_one({
        "_id": acc_id, "user_id": admin_id,
        "broker": broker, "server": server,
        "mode": "paper",
        "broker_environment": broker_env,
        "balance": balance, "equity": balance,
        "bridge_token": f"bt_221_{uuid.uuid4().hex}",
        "account_number": f"iter221_{uuid.uuid4().hex[:6]}",
    })
    return str(acc_id)


def _seed_system_cert(mongo, account_id):
    cert_id = f"cert_sys_iter221_{uuid.uuid4().hex[:10]}"
    future = (datetime.now(timezone.utc)
              + timedelta(days=90)).isoformat()
    mongo.certifications.insert_one({
        "cert_id": cert_id, "kind": "system",
        "subject": str(account_id), "passed": True,
        "checks": [], "user_id": "iter221-test",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": future, "revoked": False})
    return cert_id


def _seed_risk_truth(mongo, pid, acc_id):
    """Full v62.6 fresh evidence."""
    now = datetime.now(timezone.utc).isoformat()
    mongo.accounts.update_one(
        {"_id": ObjectId(acc_id)},
        {"$set": {"current_spreads": {"EURUSD": 0.8, "GBPUSD": 0.9,
                                      "XAUUSD": 1.2},
                  "spreads_updated_at": now}})
    mongo.pamm_programs.update_one(
        {"program_id": pid},
        {"$set": {"position_truth": {"status": "in_sync", "at": now},
                  "last_nav": {"nav": 10000.0, "at": now}}})
    mongo.pamm_nav_snapshots.delete_many({"program_id": pid})
    mongo.pamm_nav_snapshots.insert_one(
        {"program_id": pid, "nav": 10000.0, "at": now})


def _bind_master(mongo, program_id, account_id):
    mongo.pamm_programs.update_one(
        {"program_id": program_id},
        {"$set": {"master_account_id": account_id}})


def _cleanup_program(mongo, pid):
    mongo.pamm_programs.delete_one({"program_id": pid})
    mongo.pamm_master_accounts.delete_many({"program_id": pid})
    for a in list(mongo.pamm_strategy_assignments.find(
            {"pamm_program_id": pid}, {"cert_id": 1})):
        if a.get("cert_id"):
            mongo.certifications.delete_many({"cert_id": a["cert_id"]})
    mongo.pamm_strategy_assignments.delete_many({"pamm_program_id": pid})
    for c in list(mongo.strategy_cert_campaigns.find(
            {"pamm_program_id": pid}, {"campaign_id": 1, "cert_id": 1})):
        if c.get("cert_id"):
            mongo.certifications.delete_many({"cert_id": c["cert_id"]})
        mongo.strategy_cert_evidence.delete_many(
            {"campaign_id": c["campaign_id"]})
    mongo.strategy_cert_campaigns.delete_many({"pamm_program_id": pid})
    mongo.pamm_nav_snapshots.delete_many({"program_id": pid})
    mongo.pamm_risk_decisions.delete_many({"program_id": pid})
    mongo.pamm_events.delete_many({"payload.program_id": pid})


def _cleanup_account(mongo, acc_id):
    mongo.trades.delete_many({"account_id": acc_id})
    mongo.execution_intents.delete_many({"account_id": acc_id})
    mongo.certifications.delete_many(
        {"kind": "system", "subject": str(acc_id)})
    try:
        mongo.accounts.delete_one({"_id": ObjectId(acc_id)})
    except Exception:
        mongo.accounts.delete_one({"_id": acc_id})


def _assign(admin_session, pid, strategy_id="sniper",
            risk_profile_id="controlled"):
    r = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
        json={"mode": "SINGLE", "strategy_id": strategy_id,
              "risk_profile_id": risk_profile_id},
        headers=_csrf(admin_session), timeout=15)
    assert r.status_code == 200, r.text
    return r.json()


def _validate(admin_session, pid):
    r = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/strategy/validate",
        headers=_csrf(admin_session), timeout=20)
    assert r.status_code == 200, r.text


def _activate(admin_session, pid, expect=200):
    r = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/strategy/activate",
        headers=_headers(admin_session, step_up=True), timeout=15)
    assert r.status_code == expect, r.text
    return r


def _run(coro_fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        loop.close()


async def _guard_call(acc_id, signal):
    from modules.pamm.strategy_guard import (
        authorize_pamm_strategy_execution, resolve_program)
    mc = AsyncIOMotorClient(MONGO_URL)
    db = mc[DB_NAME]
    try:
        prog = await resolve_program(db, acc_id, signal)
        acc = await db.accounts.find_one({"_id": ObjectId(acc_id)})
        return await authorize_pamm_strategy_execution(
            db, prog, acc, signal)
    finally:
        mc.close()


# Good stage metrics
REPLAY_GOOD = {"decisions_replayed": 250, "determinism_ok": True,
               "expectancy_lower_r": 0.05, "risk_violations": 0}
SHADOW_GOOD = {"days_elapsed": 6, "shadow_decisions": 150,
               "agreement_rate": 0.97, "risk_violations": 0,
               "orders_placed": 0}
DEMO_GOOD = {"environment": "DEMO", "days_elapsed": 11,
             "closed_trades": 60, "unknown_rate": 0.0,
             "reject_rate": 0.01, "max_drawdown_pct": 4.0,
             "expectancy_r": 0.12}
CANARY_GOOD = {"environment": "LIVE", "canary_capital_pct": 3.0,
               "days_elapsed": 6, "closed_trades": 25,
               "max_drawdown_pct": 1.0, "critical_incidents": 0,
               "execution_health": "GREEN"}


def _walk_to_canary(admin_session, pid):
    r = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/certification/start",
        headers=_headers(admin_session), timeout=15)
    assert r.status_code == 200, r.text
    r = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/certification/advance",
        headers=_headers(admin_session, step_up=True), timeout=15)
    assert r.status_code == 200 and r.json()["state"] == "VALIDATING", r.text
    _validate(admin_session, pid)
    r = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/certification/advance",
        headers=_headers(admin_session, step_up=True), timeout=15)
    assert r.status_code == 200 and r.json()["state"] == "REPLAY", r.text
    for metrics, expected in ((REPLAY_GOOD, "SHADOW"),
                              (SHADOW_GOOD, "DEMO"),
                              (DEMO_GOOD, "CANARY")):
        admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{pid}/certification/checkpoint",
            json={"metrics": metrics},
            headers=_headers(admin_session), timeout=15)
        er = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{pid}/certification/evaluate",
            headers=_headers(admin_session), timeout=15)
        assert er.status_code == 200 and er.json()["passed"] is True, er.text
        ar = admin_session.post(
            f"{BASE_URL}/api/pamm/programs/{pid}/certification/advance",
            headers=_headers(admin_session, step_up=True), timeout=15)
        assert ar.status_code == 200, ar.text
        assert ar.json()["state"] == expected, ar.text


def _walk_to_certified(admin_session, pid):
    _walk_to_canary(admin_session, pid)
    admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/certification/checkpoint",
        json={"metrics": CANARY_GOOD},
        headers=_headers(admin_session), timeout=15)
    er = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/certification/evaluate",
        headers=_headers(admin_session), timeout=15)
    assert er.status_code == 200 and er.json()["passed"] is True, er.text
    ar = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/certification/advance",
        headers=_headers(admin_session, step_up=True), timeout=15)
    assert ar.status_code == 200 and ar.json()["state"] == "CERTIFIED", ar.text


# ═════════════════════════════════════════════════════════════════════════
# LIVE-env tests (share a class-scoped certified program to amortise the
# ~15-30s certification walk over the four LIVE-only scenarios)
# ═════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="class")
def live_setup(admin_session, admin_id):
    c = MongoClient(MONGO_URL)
    db = c[DB_NAME]
    pid = _create_program(admin_session, "live")
    acc_id = _insert_paper_account(db, admin_id, "LIVE")
    _bind_master(db, pid, acc_id)
    _assign(admin_session, pid, "sniper", "controlled")
    _seed_system_cert(db, acc_id)
    _validate(admin_session, pid)
    _walk_to_certified(admin_session, pid)
    _activate(admin_session, pid)
    yield {"pid": pid, "acc_id": acc_id, "db": db}
    _cleanup_program(db, pid)
    _cleanup_account(db, acc_id)
    c.close()


class TestLiveRiskTruth:
    """R1-R4: LIVE governed CERTIFIED program; each test resets risk-
    truth evidence in setUp and perturbs a single dimension."""

    @pytest.fixture(autouse=True)
    def _reset_evidence(self, live_setup):
        """Reset to full fresh evidence before every test in this class,
        then clear pamm_risk_decisions residuals for clean assertions."""
        _seed_risk_truth(live_setup["db"], live_setup["pid"],
                         live_setup["acc_id"])
        live_setup["db"].pamm_risk_decisions.delete_many(
            {"program_id": live_setup["pid"]})
        yield

    # R1 — risk_unknown when required evidence is missing on LIVE
    def test_r1_risk_unknown_missing_evidence(self, live_setup):
        db = live_setup["db"]
        # Wipe evidence: drop current_spreads/spreads_updated_at and NAV
        db.accounts.update_one(
            {"_id": ObjectId(live_setup["acc_id"])},
            {"$unset": {"current_spreads": "",
                        "spreads_updated_at": ""}})
        db.pamm_programs.update_one(
            {"program_id": live_setup["pid"]},
            {"$unset": {"last_nav": ""}})
        db.pamm_nav_snapshots.delete_many(
            {"program_id": live_setup["pid"]})
        out = _run(lambda: _guard_call(live_setup["acc_id"], {
            "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
            "strategy_id": "sniper", "risk_pct": 0.1}))
        assert out["authorized"] is False, out
        assert out["reason"] == "risk_unknown", out
        det = out["checks"][-1]["detail"]
        missing = det["missing_required_evidence"]
        assert "drawdown_pct" in missing, det
        assert "spread_pips" in missing, det
        assert det["state"] == "RISK_UNKNOWN"

    # R2 — position_truth missing entirely
    def test_r2_position_truth_missing(self, live_setup):
        db = live_setup["db"]
        db.pamm_programs.update_one(
            {"program_id": live_setup["pid"]},
            {"$unset": {"position_truth": ""}})
        out = _run(lambda: _guard_call(live_setup["acc_id"], {
            "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
            "strategy_id": "sniper", "risk_pct": 0.1}))
        assert out["authorized"] is False, out
        assert out["reason"] == "position_truth_missing", out

    # R3 — position_truth.at older than sniper freshness window (HIGH=600s)
    def test_r3_position_truth_stale(self, live_setup):
        db = live_setup["db"]
        old = (datetime.now(timezone.utc)
               - timedelta(minutes=20)).isoformat()
        db.pamm_programs.update_one(
            {"program_id": live_setup["pid"]},
            {"$set": {"position_truth": {"status": "in_sync",
                                         "at": old}}})
        out = _run(lambda: _guard_call(live_setup["acc_id"], {
            "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
            "strategy_id": "sniper", "risk_pct": 0.1}))
        assert out["authorized"] is False, out
        assert out["reason"] == "position_truth_stale", out
        det = out["checks"][-1]["detail"]
        assert det["age_s"] > det["max_age_s"], det

    # R4 — full fresh evidence -> authorized + snapshot doc persisted +
    #      risk_snapshot_id stamped into context
    def test_r4_authorized_and_snapshot_persisted(self, live_setup):
        db = live_setup["db"]
        out = _run(lambda: _guard_call(live_setup["acc_id"], {
            "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
            "strategy_id": "sniper", "risk_pct": 0.1}))
        assert out["authorized"] is True, out
        assert out["mode"] == "STRATEGY", out
        snap_id = out["context"]["risk_snapshot_id"]
        assert snap_id and snap_id.startswith("rds_"), out
        doc = db.pamm_risk_decisions.find_one({"snapshot_id": snap_id})
        assert doc is not None, "snapshot doc missing from DB"
        assert doc["authorized"] is True, doc
        assert doc["program_id"] == live_setup["pid"]
        assert doc["account_id"] == live_setup["acc_id"]
        assert doc["environment"] == "LIVE"
        assert doc["reason"] == "authorized"
        assert isinstance(doc.get("envelope"), dict) and doc["envelope"], doc
        assert isinstance(doc.get("telemetry"), dict) and doc["telemetry"], doc
        assert isinstance(doc.get("checks"), list) and len(doc["checks"]) > 0
        # v62.6 REQUIRED items are all resolved (non-None)
        tel = doc["telemetry"]
        for k in ("open_positions", "open_risk_pct_sum", "daily_loss_pct",
                  "weekly_loss_pct", "drawdown_pct", "spread_pips"):
            assert tel.get(k) is not None, f"REQUIRED telemetry {k} None"


# ═════════════════════════════════════════════════════════════════════════
# DEMO-env envelope checks (RISK_UNKNOWN gate does NOT apply on DEMO —
# these limits fire whenever the corresponding evidence is available)
# ═════════════════════════════════════════════════════════════════════════

class TestDemoEnvelopeCaps:

    # R5 — weekly loss cap (controlled max_weekly_loss=5.0, balance=10k,
    # seed closed trades this week with net pnl -600 -> 6.0% >= 5.0)
    def test_r5_weekly_loss_cap(self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "r5")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO",
                                       balance=10000.0)
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            # Since Monday UTC — seed a trade dated exactly midnight-Mon
            now = datetime.now(timezone.utc)
            monday = (now - timedelta(days=now.weekday())).replace(
                hour=1, minute=0, second=0, microsecond=0)
            # Ensure Monday is strictly within this week (>= week0)
            iso = monday.strftime("%Y-%m-%dT%H:%M:%S")
            for pnl in (-300.0, -300.0):
                mongo.trades.insert_one({
                    "trade_id": f"iter221_wk_{uuid.uuid4().hex[:8]}",
                    "account_id": acc_id, "status": "closed",
                    "symbol": "EURUSD", "pnl": pnl, "closed_at": iso})
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper"}))
            assert out["authorized"] is False, out
            # On Mondays the seeded Monday-01:00 trades are also TODAY's,
            # so the daily cap (checked first) fires; any other day the
            # weekly cap does.
            if now.weekday() == 0:
                assert out["reason"] == "daily_loss_cap_reached", out
            else:
                assert out["reason"] == "weekly_loss_cap_reached", out
                det = out["checks"][-1]["detail"]
                assert det["weekly_loss_pct"] >= 5.0, det
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    # R6 — drawdown cap (controlled max_drawdown=10.0; peak nav=10000,
    # current nav=8000 -> 20% drawdown)
    def test_r6_drawdown_cap(self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "r6")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            now = datetime.now(timezone.utc).isoformat()
            mongo.pamm_nav_snapshots.insert_many([
                {"program_id": pid, "nav": 10000.0, "at": now},
                {"program_id": pid, "nav": 9500.0, "at": now},
            ])
            mongo.pamm_programs.update_one(
                {"program_id": pid},
                {"$set": {"last_nav": {"nav": 8000.0, "at": now}}})
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper"}))
            assert out["authorized"] is False, out
            assert out["reason"] == "drawdown_cap_reached", out
            det = out["checks"][-1]["detail"]
            assert det["drawdown_pct"] >= 10.0, det
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    # R7a — spread cap fresh -> reject 'spread_cap_exceeded'
    def test_r7a_spread_cap_exceeded_when_fresh(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "r7a")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            now = datetime.now(timezone.utc).isoformat()
            mongo.accounts.update_one(
                {"_id": ObjectId(acc_id)},
                {"$set": {"current_spreads": {"EURUSD": 5.0},
                          "spreads_updated_at": now}})
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper"}))
            assert out["authorized"] is False, out
            assert out["reason"] == "spread_cap_exceeded", out
            det = out["checks"][-1]["detail"]
            assert det["spread_pips"] == 5.0
            assert det["max_spread_pips"] == 2.0
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    # R7b — spread STALE on DEMO -> violation is NOT fabricated
    # (spread telemetry stays None, no spread_cap_exceeded, guard passes)
    def test_r7b_spread_stale_on_demo_does_not_fabricate(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "r7b")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            stale = (datetime.now(timezone.utc)
                     - timedelta(hours=2)).isoformat()
            mongo.accounts.update_one(
                {"_id": ObjectId(acc_id)},
                {"$set": {"current_spreads": {"EURUSD": 5.0},
                          "spreads_updated_at": stale}})
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper"}))
            # DEMO passes through envelope w/o RISK_UNKNOWN gating; no
            # fabricated spread violation
            assert out["authorized"] is True, out
            # And confirm telemetry has spread_pips=None + age recorded
            snap = mongo.pamm_risk_decisions.find_one(
                {"program_id": pid, "authorized": True},
                sort=[("at", -1)])
            assert snap is not None
            assert snap["telemetry"]["spread_pips"] is None, snap["telemetry"]
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    # R8 — factor exposure (controlled max_factor_exposure=2.0)
    # Seed 1.9 lots GBPUSD open + new EURUSD 0.2 lot -> USD factor 2.1
    def test_r8_factor_exposure_exceeded(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "r8")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            mongo.trades.insert_one({
                "trade_id": f"iter221_fx_{uuid.uuid4().hex[:8]}",
                "account_id": acc_id, "status": "open",
                "symbol": "GBPUSD", "lot_size": 1.9, "risk_pct": 0.0})
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.2,
                "strategy_id": "sniper"}))
            assert out["authorized"] is False, out
            assert out["reason"] == "factor_exposure_exceeded", out
            det = out["checks"][-1]["detail"]
            assert det["factor"] == "USD", det
            assert det["proposed_lots"] > 2.0
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    # R9 — pre-trade slippage (controlled max_slippage_pips=1.0). Seed
    # closed trades w/ slippage_checked=True and slippage_pips >
    # profile max -> median > 1.0 -> reject
    def test_r9_expected_slippage_exceeded(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "r9")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            now = datetime.now(timezone.utc).isoformat()
            for slip in (2.0, 3.0, 2.5):
                mongo.trades.insert_one({
                    "trade_id": f"iter221_sl_{uuid.uuid4().hex[:8]}",
                    "account_id": acc_id, "status": "closed",
                    "symbol": "EURUSD",
                    "slippage_checked": True, "slippage_pips": slip,
                    "created_at": now})
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper"}))
            assert out["authorized"] is False, out
            assert out["reason"] == "expected_slippage_exceeded", out
            det = out["checks"][-1]["detail"]
            assert det["recent_median_slippage_pips"] > det["max_slippage_pips"]
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)


# ═════════════════════════════════════════════════════════════════════════
# R10 — Rejected decisions ALSO persist a snapshot doc
# ═════════════════════════════════════════════════════════════════════════

class TestRejectedSnapshotPersisted:

    def test_r10_rejected_decision_writes_snapshot(
            self, admin_session, admin_id, mongo):
        """Reuse an easy DEMO reject (weekly_loss_cap_reached) and verify
        pamm_risk_decisions has a doc with authorized=False and the
        reject reason."""
        pid = _create_program(admin_session, "r10")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO",
                                       balance=10000.0)
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            now = datetime.now(timezone.utc)
            monday = (now - timedelta(days=now.weekday())).replace(
                hour=1, minute=0, second=0, microsecond=0)
            iso = monday.strftime("%Y-%m-%dT%H:%M:%S")
            for pnl in (-400.0, -300.0):
                mongo.trades.insert_one({
                    "trade_id": f"iter221_r10_{uuid.uuid4().hex[:8]}",
                    "account_id": acc_id, "status": "closed",
                    "symbol": "EURUSD", "pnl": pnl, "closed_at": iso})
            mongo.pamm_risk_decisions.delete_many({"program_id": pid})
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper"}))
            assert out["authorized"] is False, out
            # weekly cap normally; on Mondays the same trades are TODAY's
            # so the daily cap (checked first) fires instead — the snapshot
            # persistence contract is what this test actually covers.
            assert out["reason"] in ("weekly_loss_cap_reached",
                                     "daily_loss_cap_reached"), out
            # Snapshot doc for a rejected decision must still be present
            docs = list(mongo.pamm_risk_decisions.find(
                {"program_id": pid}))
            assert len(docs) >= 1, "no snapshot persisted for reject"
            reject_docs = [d for d in docs if d.get("authorized") is False]
            assert len(reject_docs) >= 1, docs
            d = reject_docs[0]
            assert d["reason"] == out["reason"], d
            assert d["environment"] == "DEMO"
            assert isinstance(d.get("envelope"), dict) and d["envelope"]
            assert isinstance(d.get("telemetry"), dict) and d["telemetry"]
            assert isinstance(d.get("checks"), list) and len(d["checks"]) > 0
            # snapshot_id present
            assert d.get("snapshot_id", "").startswith("rds_"), d
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)
