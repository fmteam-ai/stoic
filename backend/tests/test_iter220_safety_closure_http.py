"""iter-220 / v62.4 — PAMM Safety Closure P0 HTTP + direct-guard tests.

Covers the user's 9 scenarios:
  S1 Migrated PAMM + assignment deleted -> FAIL CLOSED (never LEGACY).
  S2 Active sniper + signal without strategy_id -> strategy_provenance_missing.
  S3 Active sniper + signal.strategy_id='scalper' -> strategy_mismatch.
  S4 Manual override via /api/trades/manual on governed master:
       without step-up -> 401/403; with step-up bypass -> 200 with
       pamm_manual_override=true, pamm_program_id set, no pamm_strategy_id,
       origin='manual_override'.
  S5 CANARY capital cap enforced at execution:
       canary_open_risk_cap_exceeded / authorized / canary_requires_risk_pct.
  S6 Material patch on CANARY -> campaign reset/revoked; forced-restore
     CANARY state with OLD identity hash -> canary_identity_drift.
  S7 CERTIFIED, account.broker_server changed -> certification_identity_drift.
  S8 Custom risk profile allowed_symbols=['XAUUSD']: EURUSD -> symbol_not_allowed,
     XAUUSD -> authorized.
  S9 Closed trades pnl=-250 today, balance=10k, controlled profile
     max_daily_loss=2.0 -> daily_loss_cap_reached.
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

# Ensure backend/ is importable for direct guard/campaign calls
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.http

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
STEP_UP_BYPASS = os.environ.get("STEP_UP_BYPASS_TOKEN", "")

ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PASSWORD = "admin123"


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


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
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
    name = f"TEST_iter220_{suffix}_{uuid.uuid4().hex[:6]}"
    r = admin_session.post(f"{BASE_URL}/api/pamm/programs",
                           json={"name": name},
                           headers=_csrf(admin_session), timeout=20)
    assert r.status_code in (200, 201), r.text
    return r.json()["program_id"]


def _insert_paper_account(mongo, admin_id, broker_env="DEMO",
                         broker="TestBroker220",
                         server="TestServer220-A", balance=10000.0):
    acc_id = ObjectId()
    mongo.accounts.insert_one({
        "_id": acc_id, "user_id": admin_id,
        "broker": broker, "server": server,
        "mode": "paper",
        "broker_environment": broker_env,
        "balance": balance, "equity": balance,
        "bridge_token": f"bt_220_{uuid.uuid4().hex}",
        "account_number": f"iter220_{uuid.uuid4().hex[:6]}",
    })
    return str(acc_id)


def _seed_system_cert(mongo, account_id):
    """Seed a valid kind:'system' broker certification bound to the
    account. Required because sniper/scalper/fast_scalp all have
    requires_broker_certification=True in the registry — on LIVE the
    guard demands a system cert before it evaluates the risk envelope."""
    cert_id = f"cert_sys_iter220_{uuid.uuid4().hex[:10]}"
    future = (datetime.now(timezone.utc)
              + timedelta(days=90)).isoformat()
    mongo.certifications.insert_one({
        "cert_id": cert_id, "kind": "system",
        "subject": str(account_id), "passed": True,
        "checks": [], "user_id": "iter220-test",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": future, "revoked": False})
    return cert_id


def _seed_risk_truth(mongo, pid, acc_id):
    """v62.6 — LIVE governed executions require COMPLETE risk evidence:
    fresh spreads, NAV history (drawdown), fresh position truth."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        mongo.accounts.update_one(
            {"_id": ObjectId(acc_id)},
            {"$set": {"current_spreads": {"EURUSD": 0.8, "GBPUSD": 0.9,
                                          "XAUUSD": 1.2},
                      "spreads_updated_at": now}})
    except Exception:
        pass
    mongo.pamm_programs.update_one(
        {"program_id": pid},
        {"$set": {"position_truth": {"status": "in_sync", "at": now},
                  "last_nav": {"nav": 10000.0, "at": now}}})
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
    """Run an async coroutine in a fresh event loop (isolates motor client)."""
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


# Good stage metrics (from iter217)
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
    """Assumes program has assignment + LIVE bound master. Walks the
    campaign DRAFT → VALIDATING → REPLAY → SHADOW → DEMO → CANARY."""
    r = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/certification/start",
        headers=_headers(admin_session), timeout=15)
    assert r.status_code == 200, r.text
    # DRAFT -> VALIDATING
    r = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/certification/advance",
        headers=_headers(admin_session, step_up=True), timeout=15)
    assert r.status_code == 200 and r.json()["state"] == "VALIDATING", r.text
    _validate(admin_session, pid)
    # VALIDATING -> REPLAY
    r = admin_session.post(
        f"{BASE_URL}/api/pamm/programs/{pid}/certification/advance",
        headers=_headers(admin_session, step_up=True), timeout=15)
    assert r.status_code == 200 and r.json()["state"] == "REPLAY", r.text
    for metrics, expected_state in ((REPLAY_GOOD, "SHADOW"),
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
        assert ar.json()["state"] == expected_state, ar.text


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


# ═════ S1 · Migrated PAMM + assignment deleted -> FAIL CLOSED ═════════════

class TestS1MigratedFailClosed:
    def test_s1a_active_deleted_leaves_replaced_get_shows_governed(
            self, admin_session, admin_id, mongo):
        """Delete ONLY the ACTIVE assignment but keep a REPLACED history
        doc; GET /strategy should return mode 'GOVERNED_NO_ACTIVE' and
        the guard MUST reject with governed_program_requires_assignment."""
        pid = _create_program(admin_session, "s1a")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            # Flip the ACTIVE assignment to REPLACED so a history doc remains
            r = mongo.pamm_strategy_assignments.update_many(
                {"pamm_program_id": pid, "status": "ACTIVE"},
                {"$set": {"status": "REPLACED"}})
            assert r.modified_count >= 1
            # GET /strategy → GOVERNED_NO_ACTIVE
            g = admin_session.get(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                timeout=15)
            assert g.status_code == 200, g.text
            body = g.json()
            assert body["mode"] == "GOVERNED_NO_ACTIVE", body
            assert body.get("fail_closed") is True, body
            assert body["assignment"] is None
            # Direct guard call → REJECT (never legacy_mode)
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper"}))
            assert out["authorized"] is False, out
            assert out["reason"] == "governed_program_requires_assignment", out
            assert out["mode"] == "STRATEGY", out
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)

    def test_s1b_all_deleted_sticky_governance_still_rejects(
            self, admin_session, admin_id, mongo):
        """DELETE every assignment doc for the program.
        Guard MUST still fail closed via program.strategy_governance sticky
        flag (never revert to LEGACY)."""
        pid = _create_program(admin_session, "s1b")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            # Verify sticky flag set
            prog = mongo.pamm_programs.find_one({"program_id": pid})
            assert prog.get("strategy_governance") == "STRATEGY", prog
            # DELETE all assignment docs
            mongo.pamm_strategy_assignments.delete_many(
                {"pamm_program_id": pid})
            # Direct guard call — sticky flag still forces STRATEGY mode
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper"}))
            assert out["authorized"] is False, out
            assert out["reason"] == "governed_program_requires_assignment", out
            assert out["mode"] == "STRATEGY", out
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)


# ═════ S2 · signal WITHOUT strategy_id -> strategy_provenance_missing ═════

class TestS2ProvenanceMissing:
    def test_signal_without_strategy_id_rejected(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "s2")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY",
                "lot_size": 0.01}))  # NO strategy_id
            assert out["authorized"] is False, out
            assert out["reason"] == "strategy_provenance_missing", out
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)


# ═════ S3 · signal.strategy_id != assignment.strategy_id -> mismatch ══════

class TestS3StrategyMismatch:
    def test_signal_mismatch_rejected(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "s3")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "scalper"}))
            assert out["authorized"] is False, out
            assert out["reason"] == "strategy_mismatch", out
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)


# ═════ S4 · Manual override HTTP path ═════════════════════════════════════

class TestS4ManualOverrideHTTP:
    def _fire(self, admin_session, account_id):
        return admin_session.post(
            f"{BASE_URL}/api/trades/manual",
            json={"account_id": account_id, "symbol": "EURUSD",
                  "action": "BUY", "lot_size": 0.01,
                  "sl_pips": 20, "tp1_pips": 10, "tp2_pips": 20,
                  "tp3_pips": 30},
            headers=_csrf(admin_session), timeout=25)

    def test_manual_override_flow(self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "s4")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            # 1) WITHOUT step-up bypass -> 401/403
            prev = admin_session.headers.get("X-Step-Up-Bypass")
            admin_session.headers["X-Step-Up-Bypass"] = ""
            try:
                r = self._fire(admin_session, acc_id)
            finally:
                if prev is None:
                    admin_session.headers.pop("X-Step-Up-Bypass", None)
                else:
                    admin_session.headers["X-Step-Up-Bypass"] = prev
            assert r.status_code in (401, 403), r.text
            # 2) WITH step-up bypass (conftest auto-injects) -> 200
            r = self._fire(admin_session, acc_id)
            assert r.status_code == 200, r.text
            body = r.json()
            assert not body.get("blocked"), body
            assert body.get("pamm_manual_override") is True, body
            assert body.get("pamm_program_id") == pid, body
            assert not body.get("pamm_strategy_id"), body
            assert body.get("origin") == "manual_override", body
        finally:
            mongo.trades.delete_many({"account_id": acc_id})
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)


# ═════ S5 · CANARY capital cap enforced at execution ══════════════════════

class TestS5CanaryCapitalCap:
    @pytest.fixture(scope="class")
    def canary_setup(self, admin_session, admin_id):
        """Build a LIVE-env program in CANARY state with ACTIVE assignment
        and seed open trades summing risk_pct=4.9."""
        c = MongoClient(MONGO_URL)
        mongo = c[DB_NAME]
        pid = _create_program(admin_session, "s5")
        acc_id = _insert_paper_account(mongo, admin_id, "LIVE")
        _bind_master(mongo, pid, acc_id)
        _assign(admin_session, pid, "sniper", "controlled")
        _seed_system_cert(mongo, acc_id)
        _validate(admin_session, pid)
        _walk_to_canary(admin_session, pid)
        _activate(admin_session, pid)
        _seed_risk_truth(mongo, pid, acc_id)
        # Seed 3 open trades summing risk_pct=4.9
        for r in (1.7, 1.6, 1.6):
            mongo.trades.insert_one({
                "trade_id": f"iter220_open_{uuid.uuid4().hex[:8]}",
                "account_id": acc_id, "status": "open",
                "symbol": "GBPUSD",  # different symbol from tests
                "lot_size": 0.01, "risk_pct": r})
        yield {"pid": pid, "acc_id": acc_id, "mongo": mongo}
        _cleanup_program(mongo, pid)
        _cleanup_account(mongo, acc_id)
        c.close()

    def test_canary_open_risk_cap_exceeded(self, canary_setup):
        out = _run(lambda: _guard_call(canary_setup["acc_id"], {
            "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
            "strategy_id": "sniper", "risk_pct": 0.4}))
        assert out["authorized"] is False, out
        assert out["reason"] == "canary_open_risk_cap_exceeded", out
        det = out["checks"][-1]["detail"]
        assert abs(det["proposed_total"] - 5.3) < 0.01, det

    def test_canary_within_cap_authorized(self, canary_setup):
        out = _run(lambda: _guard_call(canary_setup["acc_id"], {
            "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
            "strategy_id": "sniper", "risk_pct": 0.05}))
        assert out["authorized"] is True, out
        assert out["context"]["canary_mode"] is True, out

    def test_canary_missing_risk_pct_rejected(self, canary_setup):
        out = _run(lambda: _guard_call(canary_setup["acc_id"], {
            "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
            "strategy_id": "sniper"}))  # no risk_pct
        assert out["authorized"] is False, out
        assert out["reason"] == "canary_requires_risk_pct", out


# ═════ S6 · Material patch on CANARY -> campaign reset + identity drift ═══

class TestS6MaterialPatchResetsCampaign:
    def test_patch_resets_campaign_then_restored_identity_drifts(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "s6")
        acc_id = _insert_paper_account(mongo, admin_id, "LIVE")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _seed_system_cert(mongo, acc_id)
            _validate(admin_session, pid)
            _walk_to_canary(admin_session, pid)
            _activate(admin_session, pid)
            # Snapshot old identity hash for later restore
            camp_before = mongo.strategy_cert_campaigns.find_one(
                {"pamm_program_id": pid, "state": "CANARY"})
            assert camp_before is not None, "campaign should be CANARY"
            old_identity = camp_before["identity"]
            old_identity_hash = old_identity["identity_hash"]
            # PATCH risk profile -> material change should reset campaign
            r = admin_session.patch(
                f"{BASE_URL}/api/pamm/programs/{pid}/strategy",
                json={"risk_profile_id": "growth"},
                headers=_headers(admin_session, step_up=True), timeout=15)
            assert r.status_code == 200, r.text
            # GET /certification -> state is DRAFT or REVOKED
            gc = admin_session.get(
                f"{BASE_URL}/api/pamm/programs/{pid}/certification",
                timeout=15)
            assert gc.status_code == 200
            body = gc.json()
            camp = body.get("campaign")
            # Campaign should have been aborted mid-pipeline -> DRAFT
            # (transition_allowed(CANARY, DRAFT) => True)
            if camp is not None:
                assert camp["state"] in ("DRAFT", "REVOKED"), camp
            # Now manually restore CANARY with the OLD identity hash so
            # the guard sees a canary campaign with an obsolete identity.
            # Assignment now has risk_profile_id=growth so re-derived hash
            # will not match old_identity_hash.
            camp_id = camp_before["campaign_id"]
            mongo.strategy_cert_campaigns.update_one(
                {"campaign_id": camp_id},
                {"$set": {"state": "CANARY",
                          "identity": old_identity}})
            # Ensure the current assignment is ACTIVE so the guard reaches
            # the certification identity check. The material patch clears
            # last_validation and flips certification_status; re-validate
            # so we can activate cleanly.
            mongo.pamm_strategy_assignments.update_one(
                {"pamm_program_id": pid,
                 "status": {"$in": ["ASSIGNED", "SUSPENDED", "ACTIVE"]}},
                {"$set": {"status": "ACTIVE"}})
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper", "risk_pct": 0.05}))
            assert out["authorized"] is False, out
            assert out["reason"] == "canary_identity_drift", out
            det = out["checks"][-1]["detail"]
            assert det["campaign"] == old_identity_hash
            assert det["current"] != old_identity_hash
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)


# ═════ S7 · Certified combo then broker_server change -> drift ═══════════

class TestS7CertifiedThenBrokerMove:
    def test_broker_server_move_causes_identity_drift(
            self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "s7")
        acc_id = _insert_paper_account(mongo, admin_id, "LIVE",
                                       server="Srv-A")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _seed_system_cert(mongo, acc_id)
            _validate(admin_session, pid)
            _walk_to_certified(admin_session, pid)
            _activate(admin_session, pid)
            _seed_risk_truth(mongo, pid, acc_id)
            # Direct guard should PASS while server matches
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper", "risk_pct": 0.1}))
            assert out["authorized"] is True, out
            # Move the account to a different broker server
            mongo.accounts.update_one(
                {"_id": ObjectId(acc_id)},
                {"$set": {"server": "Srv-B"}})
            out2 = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper", "risk_pct": 0.1}))
            assert out2["authorized"] is False, out2
            assert out2["reason"] == "certification_identity_drift", out2
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)


# ═════ S8 · Custom allowed_symbols envelope ═══════════════════════════════

CUSTOM_PROFILE_ID = "iter220_gold_only"


class TestS8AllowedSymbols:
    @pytest.fixture(scope="class", autouse=True)
    def seed_profile(self):
        c = MongoClient(MONGO_URL)
        db = c[DB_NAME]
        db.pamm_risk_profiles.update_one(
            {"risk_profile_id": CUSTOM_PROFILE_ID},
            {"$set": {"risk_profile_id": CUSTOM_PROFILE_ID,
                      "display_name": "GoldOnly",
                      "max_risk_per_trade": 0.5,
                      "max_daily_loss": 2.0, "max_weekly_loss": 5.0,
                      "max_drawdown": 10.0,
                      "max_open_positions": 4,
                      "max_symbol_exposure": 1.0,
                      "max_factor_exposure": 2.0,
                      "max_consecutive_losses": 4,
                      "allowed_symbols": ["XAUUSD"],
                      "spread_limits": {"max_spread_pips": 2.0},
                      "slippage_limits": {"max_slippage_pips": 1.0},
                      "emergency_stop_rules": {"daily_loss_pct": 2.0}}},
            upsert=True)
        yield
        db.pamm_risk_profiles.delete_one(
            {"risk_profile_id": CUSTOM_PROFILE_ID})
        c.close()

    def test_symbol_gating(self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "s8")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO")
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", CUSTOM_PROFILE_ID)
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            out_bad = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper"}))
            assert out_bad["authorized"] is False, out_bad
            assert out_bad["reason"] == "symbol_not_allowed", out_bad
            out_ok = _run(lambda: _guard_call(acc_id, {
                "symbol": "XAUUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper"}))
            assert out_ok["authorized"] is True, out_ok
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)


# ═════ S9 · Daily loss cap reached ════════════════════════════════════════

class TestS9DailyLossCap:
    def test_daily_loss_cap_rejects(self, admin_session, admin_id, mongo):
        pid = _create_program(admin_session, "s9")
        acc_id = _insert_paper_account(mongo, admin_id, "DEMO",
                                       balance=10000.0)
        _bind_master(mongo, pid, acc_id)
        try:
            _assign(admin_session, pid, "sniper", "controlled")
            _validate(admin_session, pid)
            _activate(admin_session, pid)
            # Seed 3 closed trades TODAY summing pnl -250
            midnight = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S")
            for pnl in (-100.0, -100.0, -50.0):
                mongo.trades.insert_one({
                    "trade_id": f"iter220_cls_{uuid.uuid4().hex[:8]}",
                    "account_id": acc_id, "status": "closed",
                    "symbol": "EURUSD", "pnl": pnl,
                    "closed_at": midnight})
            out = _run(lambda: _guard_call(acc_id, {
                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
                "strategy_id": "sniper"}))
            assert out["authorized"] is False, out
            assert out["reason"] == "daily_loss_cap_reached", out
        finally:
            _cleanup_program(mongo, pid)
            _cleanup_account(mongo, acc_id)
