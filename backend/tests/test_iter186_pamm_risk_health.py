"""iter-186 — PAMM Phase 9 Risk Engine + Phase 10 Broker Health Monitor.
Loss caps / drawdown / exposure / correlation gates, halt vs flatten,
news blackout, heartbeat + 0-100 health score."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"
TIMEOUT = 25


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


def _create_program(s, name):
    r = s.post(f"{API}/pamm/programs", json={"name": name}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()


def _cleanup(program_id):
    db = _db()
    p = _run(db.pamm_programs.find_one({"program_id": program_id})) or {}
    bpid = p.get("broker_program_id")
    for c in ("pamm_programs", "pamm_master_accounts", "pamm_allocations",
              "pamm_nav_snapshots", "pamm_reconciliation", "pamm_audit"):
        _run(getattr(db, c).delete_many({"program_id": program_id}))
    if bpid:
        for c in ("sandbox_broker_programs", "sandbox_broker_investors",
                  "sandbox_broker_allocations", "sandbox_broker_positions"):
            _run(getattr(db, c).delete_many({"program_id": bpid}))


def _iso(dt):
    return dt.isoformat()


def _seed_navs(program_id, navs):
    """navs = [(nav, dt)]"""
    db = _db()
    _run(db.pamm_nav_snapshots.insert_many(
        [{"program_id": program_id, "nav": float(n), "currency": "USD",
          "at": _iso(d)} for n, d in navs]))
    last = max(navs, key=lambda x: x[1])
    _run(db.pamm_programs.update_one(
        {"program_id": program_id},
        {"$set": {"last_nav": {"nav": float(last[0]), "at": _iso(last[1])}}}))


class TestRiskLimitsConfig:
    def test_get_default_limits(self):
        s = _admin()
        prog = _create_program(s, f"risk-cfg-{uuid.uuid4().hex[:6]}")
        try:
            r = s.get(f"{API}/pamm/programs/{prog['program_id']}/risk-limits",
                      timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            lim = r.json()["risk_limits"]
            assert lim["daily_loss_pct"]["threshold"] == 5.0
            assert lim["max_drawdown_pct"]["action"] == "flatten"
            assert lim["news_filter"]["enabled"] is True
        finally:
            _cleanup(prog["program_id"])

    def test_put_limits_and_validation(self):
        s = _admin()
        prog = _create_program(s, f"risk-put-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            r = s.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"daily_loss_pct": {"threshold": 2.5,
                                               "action": "flatten"}},
                      timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            assert r.json()["risk_limits"]["daily_loss_pct"]["threshold"] == 2.5
            assert r.json()["risk_limits"]["daily_loss_pct"]["action"] == "flatten"
            # unknown limit rejected
            r = s.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"bogus": {"threshold": 1}}, timeout=TIMEOUT)
            assert r.status_code == 400
            # bad action rejected
            r = s.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"daily_loss_pct": {"action": "nuke"}},
                      timeout=TIMEOUT)
            assert r.status_code == 400
            # negative threshold rejected
            r = s.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"daily_loss_pct": {"threshold": -3}},
                      timeout=TIMEOUT)
            assert r.status_code == 400
        finally:
            _cleanup(pid)


class TestRiskEvaluation:
    def test_daily_loss_breach_halts(self):
        s = _admin()
        prog = _create_program(s, f"risk-halt-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            now = datetime.now(timezone.utc)
            _seed_navs(pid, [(100000, now - timedelta(days=2)),
                             (90000, now)])  # -10% today vs carry-over base
            r = s.post(f"{API}/pamm/programs/{pid}/risk-check",
                       timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            body = r.json()
            breached = {c["limit"] for c in body["breached"]}
            assert "daily_loss_pct" in breached
            assert body["action_taken"] == "halt"
            # program is paused with a recorded breach
            p = _run(_db().pamm_programs.find_one({"program_id": pid}))
            assert p["trading"] == "paused"
            assert "daily_loss_pct" in p["risk_breach"]["limits"]
            # trading_allowed reflects the breach
            r = s.get(f"{API}/pamm/programs/{pid}", timeout=TIMEOUT)
            assert r.json()["trading_allowed"] is False
            assert "risk_breach" in r.json()["trading_block_reason"]
            # RiskLimitBreached event emitted + admin notification raised
            ev = _run(_db().pamm_events.find_one(
                {"type": "RiskLimitBreached", "data.program_id": pid}))
            assert ev is not None
            note = _run(_db().pamm_notifications.find_one(
                {"type": "RiskLimitBreached", "program_id": pid}))
            assert note is not None
        finally:
            _run(_db().pamm_events.delete_many({"data.program_id": pid}))
            _run(_db().pamm_notifications.delete_many({"program_id": pid}))
            _cleanup(pid)

    def test_drawdown_breach_flattens_positions(self):
        s = _admin()
        prog = _create_program(s, f"risk-flat-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            bpid = p["broker_program_id"]
            # open sandbox positions to be flattened
            _run(db.sandbox_broker_positions.insert_many([
                {"position_id": f"pos_{i}", "program_id": bpid,
                 "symbol": "EURUSD", "volume": 1.0, "exposure_usd": 1000}
                for i in range(2)]))
            now = datetime.now(timezone.utc)
            # disable loss caps so ONLY drawdown (action=flatten) fires
            r = s.put(f"{API}/pamm/programs/{pid}/risk-limits",
                      json={"daily_loss_pct": {"enabled": False},
                            "weekly_loss_pct": {"enabled": False},
                            "monthly_loss_pct": {"enabled": False},
                            "max_drawdown_pct": {"threshold": 10.0}},
                      timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            _seed_navs(pid, [(100000, now - timedelta(days=40)),
                             (85000, now)])  # 15% dd from peak
            r = s.post(f"{API}/pamm/programs/{pid}/risk-check",
                       timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["action_taken"] == "flatten"
            assert body["risk_breach"]["flattened"] == 2
            left = _run(db.sandbox_broker_positions.count_documents(
                {"program_id": bpid}))
            assert left == 0
            ev = _run(db.pamm_events.find_one(
                {"type": "PositionsFlattened", "data.program_id": pid}))
            assert ev is not None
        finally:
            _run(db.pamm_events.delete_many({"data.program_id": pid}))
            _run(db.pamm_notifications.delete_many({"program_id": pid}))
            _cleanup(pid)

    def test_exposure_and_correlation_checks(self):
        s = _admin()
        prog = _create_program(s, f"risk-exp-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            p = _run(db.pamm_programs.find_one({"program_id": pid}))
            bpid = p["broker_program_id"]
            now = datetime.now(timezone.utc)
            _seed_navs(pid, [(100000, now)])
            _run(db.sandbox_broker_positions.insert_many([
                {"position_id": f"p{i}", "program_id": bpid, "symbol": sym,
                 "exposure_usd": 60000}
                for i, sym in enumerate(
                    ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD"])]))
            r = s.get(f"{API}/pamm/programs/{pid}/risk-status",
                      timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            checks = {c["limit"]: c for c in r.json()["checks"]}
            assert checks["max_exposure_pct"]["value"] == 240.0
            assert checks["max_exposure_pct"]["breached"] is True
            assert checks["max_correlated_positions"]["value"] == 4  # USD x4
            assert checks["max_correlated_positions"]["breached"] is True
            # risk-status is PURE: program must still be enabled
            p2 = _run(db.pamm_programs.find_one({"program_id": pid}))
            assert p2["trading"] == "enabled"
            assert not p2.get("risk_breach")
        finally:
            _cleanup(pid)

    def test_clear_breach_then_resume(self):
        s = _admin()
        prog = _create_program(s, f"risk-clr-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            now = datetime.now(timezone.utc)
            _seed_navs(pid, [(100000, now - timedelta(days=2)), (90000, now)])
            s.post(f"{API}/pamm/programs/{pid}/risk-check", timeout=TIMEOUT)
            r = s.post(f"{API}/pamm/programs/{pid}/clear-risk-breach",
                       timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            r = s.post(f"{API}/pamm/programs/{pid}/resume", timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            p = _run(_db().pamm_programs.find_one({"program_id": pid}))
            assert p["trading"] == "enabled"
            assert not p.get("risk_breach")
        finally:
            _run(_db().pamm_events.delete_many({"data.program_id": pid}))
            _run(_db().pamm_notifications.delete_many({"program_id": pid}))
            _cleanup(pid)

    def test_no_breach_on_healthy_program(self):
        s = _admin()
        prog = _create_program(s, f"risk-ok-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        try:
            now = datetime.now(timezone.utc)
            _seed_navs(pid, [(100000, now - timedelta(days=1)),
                             (100500, now)])
            r = s.post(f"{API}/pamm/programs/{pid}/risk-check",
                       timeout=TIMEOUT)
            assert r.status_code == 200
            assert r.json()["breached"] == []
            assert r.json()["action_taken"] is None
            p = _run(_db().pamm_programs.find_one({"program_id": pid}))
            assert p["trading"] == "enabled"
        finally:
            _cleanup(pid)


class TestNewsFilter:
    def test_blackout_window_math(self):
        """Unit-level: an injected cached high-impact event blocks trading."""
        from modules.pamm.risk.news import news_blackout_status
        db = _db()
        now = datetime.now(timezone.utc)
        _run(db.pamm_news_cache.replace_one(
            {"_id": "ff_thisweek"},
            {"_id": "ff_thisweek", "fetched_at": now.isoformat(),
             "events": [
                 {"title": "NFP", "country": "USD", "impact": "high",
                  "date": (now + timedelta(minutes=10)).isoformat()},
                 {"title": "Minor PMI", "country": "EUR", "impact": "low",
                  "date": (now + timedelta(minutes=5)).isoformat()},
             ]}, upsert=True))
        try:
            cfg = {"enabled": True, "blackout_before_min": 30,
                   "blackout_after_min": 15, "min_impact": "high"}
            st = _run(news_blackout_status(db, cfg))
            assert st["active"] is True
            assert st["event"]["title"] == "NFP"
            # low-impact event alone must NOT trigger at min_impact=high
            cfg2 = dict(cfg, blackout_before_min=1)
            st2 = _run(news_blackout_status(db, cfg2))
            assert st2["active"] is False
            # disabled filter never blocks
            st3 = _run(news_blackout_status(db, {"enabled": False}))
            assert st3["active"] is False
        finally:
            _run(db.pamm_news_cache.delete_many({"_id": "ff_thisweek"}))

    def test_news_endpoint(self):
        s = _admin()
        r = s.get(f"{API}/pamm/news", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "high_impact" in body and "source" in body


class TestBrokerHealth:
    def test_heartbeat_and_score(self):
        s = _admin()
        r = s.post(f"{API}/pamm/health/check", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        results = r.json()["results"]
        sbx = next(x for x in results if x["partner_id"] == "prt_sandbox")
        assert sbx["ok"] is True
        assert sbx["latency_ms"] >= 0
        assert 0 <= sbx["score"] <= 100
        assert sbx["status"] in ("healthy", "degraded", "down")

    def test_health_overview(self):
        s = _admin()
        s.post(f"{API}/pamm/health/check", timeout=TIMEOUT)
        r = s.get(f"{API}/pamm/health", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        partners = r.json()["partners"]
        sbx = next(p for p in partners if p["partner_id"] == "prt_sandbox")
        assert "webhook_secret_enc" not in sbx  # secrets never leak
        assert sbx["health"]["score"] > 0
        assert len(sbx["history"]) >= 1

    def test_score_formula(self):
        from services.broker_gateway.health import compute_score, status_for
        perfect = [{"ok": True, "latency_ms": 50}] * 20
        assert compute_score(perfect) == 100.0
        dead = [{"ok": False, "latency_ms": 0}] * 20
        assert compute_score(dead) == 0.0
        assert status_for(85) == "healthy"
        assert status_for(60) == "degraded"
        assert status_for(20) == "down"

    def test_heartbeat_failure_records_event(self):
        """A broken partner produces ok=False, HeartbeatLost + notification."""
        from services.broker_gateway.health import heartbeat_partner
        db = _db()
        fake = {"partner_id": f"prt_test_{uuid.uuid4().hex[:6]}",
                "name": "Broken Broker", "adapter": "no-such-adapter"}
        _run(db.broker_partners.insert_one(dict(fake)))
        try:
            res = _run(heartbeat_partner(db, fake))
            assert res["ok"] is False
            assert res["status"] == "down"
            ev = _run(db.pamm_events.find_one(
                {"type": "HeartbeatLost",
                 "data.partner_id": fake["partner_id"]}))
            assert ev is not None
            note = _run(db.pamm_notifications.find_one(
                {"type": "HeartbeatLost",
                 "partner_id": fake["partner_id"]}))
            assert note is not None
        finally:
            _run(db.broker_partners.delete_many(
                {"partner_id": fake["partner_id"]}))
            _run(db.pamm_health.delete_many(
                {"partner_id": fake["partner_id"]}))
            _run(db.pamm_events.delete_many(
                {"data.partner_id": fake["partner_id"]}))
            _run(db.pamm_notifications.delete_many(
                {"partner_id": fake["partner_id"]}))


class TestAccessControl:
    def test_non_manager_cannot_touch_risk_or_health(self):
        r = requests.get(f"{API}/pamm/health", timeout=TIMEOUT)
        assert r.status_code in (401, 403)
        r = requests.get(f"{API}/pamm/news", timeout=TIMEOUT)
        assert r.status_code in (401, 403)
        r = requests.post(f"{API}/pamm/programs/pgm_x/risk-check",
                          timeout=TIMEOUT)
        assert r.status_code in (401, 403)
