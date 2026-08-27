"""iter-193 — v55 batch: execution intents (at-most-once idempotency),
real broker adapters (generic REST + MT5 Manager) with certification,
and Position Truth reconciliation (drift → freeze new exposure)."""
import os
import sys
import uuid

import pytest
import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ADMIN = ("admin@trading.bot", "admin123")
TIMEOUT = 30


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _login():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN[0], "password": ADMIN[1]},
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
              "pamm_nav_snapshots", "pamm_reconciliation", "pamm_audit",
              "pamm_incidents", "pamm_position_truth",
              "pamm_expected_positions", "execution_intents"):
        _run(getattr(db, c).delete_many({"program_id": program_id}))
    _run(db.pamm_events.delete_many({"data.program_id": program_id}))
    _run(db.pamm_notifications.delete_many({"program_id": program_id}))
    if bpid:
        for c in ("sandbox_broker_programs", "sandbox_broker_investors",
                  "sandbox_broker_allocations", "sandbox_broker_positions"):
            _run(getattr(db, c).delete_many({"program_id": bpid}))


# ---------------------------------------------------------------- intents
class TestExecutionIntents:
    def test_intent_id_format_and_ordering(self):
        from execution_intents import new_intent_id
        a, b = new_intent_id(), new_intent_id()
        assert a.startswith("xin_") and len(a) == 30
        assert a != b

    def test_create_intent_dedupes(self):
        from execution_intents import create_intent, dedupe_key_for
        db = _db()
        key = dedupe_key_for("test", "open_trade", uuid.uuid4().hex)
        try:
            first = _run(create_intent(db, source="test", kind="open_trade",
                                       dedupe_key=key, payload={"x": 1}))
            second = _run(create_intent(db, source="test",
                                        kind="open_trade", dedupe_key=key))
            assert first["duplicate"] is False
            assert second["duplicate"] is True
            assert second["intent_id"] == first["intent_id"]
            n = _run(db.execution_intents.count_documents(
                {"dedupe_key": key}))
            assert n == 1
        finally:
            _run(db.execution_intents.delete_many({"dedupe_key": key}))

    def test_run_once_executes_at_most_once(self):
        from execution_intents import dedupe_key_for, run_once
        db = _db()
        key = dedupe_key_for("test", "flatten", uuid.uuid4().hex)
        calls = []

        async def executor(intent):
            calls.append(intent["intent_id"])
            return {"closed": 3}
        try:
            first = _run(run_once(db, source="test", kind="flatten",
                                  dedupe_key=key, executor=executor))
            second = _run(run_once(db, source="test", kind="flatten",
                                   dedupe_key=key, executor=executor))
            assert len(calls) == 1, "executor re-ran on retry!"
            assert first["status"] == "filled"
            assert first["result"] == {"closed": 3}
            assert second["duplicate"] is True
            assert second["result"] == {"closed": 3}
            assert second["in_flight"] is False
        finally:
            _run(db.execution_intents.delete_many({"dedupe_key": key}))

    def test_run_once_failure_marks_rejected(self):
        """Broker-CONFIRMED errors (parsed responses → ValueError) reject."""
        from execution_intents import dedupe_key_for, run_once
        db = _db()
        key = dedupe_key_for("test", "flatten", uuid.uuid4().hex)

        async def boom(intent):
            raise ValueError("broker rejected: invalid volume")
        try:
            with pytest.raises(ValueError):
                _run(run_once(db, source="test", kind="flatten",
                              dedupe_key=key, executor=boom))
            doc = _run(db.execution_intents.find_one({"dedupe_key": key}))
            assert doc["status"] == "rejected"
            assert "broker rejected" in (doc.get("result") or {}).get(
                "error", "")
        finally:
            _run(db.execution_intents.delete_many({"dedupe_key": key}))

    def test_lifecycle_transitions(self):
        from execution_intents import create_intent, transition
        db = _db()
        intent = _run(create_intent(db, source="test", kind="open_trade"))
        iid = intent["intent_id"]
        try:
            assert _run(transition(db, iid, "submitted")) is not None
            assert _run(transition(db, iid, "acked")) is not None
            filled = _run(transition(db, iid, "filled",
                                     result={"ticket": 42}))
            assert filled["status"] == "filled"
            # terminal — illegal transitions are refused
            assert _run(transition(db, iid, "submitted")) is None
            assert _run(transition(db, iid, "rejected")) is None
            doc = _run(db.execution_intents.find_one({"intent_id": iid}))
            assert doc["status"] == "filled"
            assert [h["to"] for h in doc["history"]] == [
                "created", "submitted", "acked", "filled"]
        finally:
            _run(db.execution_intents.delete_many({"intent_id": iid}))

    def test_expire_stale(self):
        from execution_intents import create_intent, expire_stale
        db = _db()
        intent = _run(create_intent(db, source="test", kind="open_trade"))
        iid = intent["intent_id"]
        try:
            _run(db.execution_intents.update_one(
                {"intent_id": iid},
                {"$set": {"created_at": "2020-01-01T00:00:00+00:00"}}))
            n = _run(expire_stale(db))
            assert n >= 1
            doc = _run(db.execution_intents.find_one({"intent_id": iid}))
            assert doc["status"] == "expired"
        finally:
            _run(db.execution_intents.delete_many({"intent_id": iid}))


class TestFlattenIntentIdempotency:
    def test_same_flatten_attempt_executes_once(self):
        from modules.pamm.risk.states import _flatten_and_verify
        s = _login()
        prog = _create_program(s, f"fli-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            assert _run(_flatten_and_verify(db, p, "test", attempt=1))
            assert _run(_flatten_and_verify(db, p, "test", attempt=1))
            n = _run(db.execution_intents.count_documents(
                {"kind": "emergency_flatten", "program_id": pid}))
            assert n == 1, "duplicate flatten attempt minted a 2nd intent"
            exp = _run(db.pamm_expected_positions.find_one(
                {"program_id": pid}))
            assert exp is not None and exp["positions"] == []
        finally:
            _cleanup(pid)


# --------------------------------------------------------- REST adapter
class TestRestAdapterAndCertification:
    def test_rest_demo_partner_seeded(self):
        db = _db()
        p = _run(db.broker_partners.find_one(
            {"partner_id": "prt_rest_demo"}, {"_id": 0}))
        assert p and p["adapter"] == "rest"
        assert p["rest_config"].get("api_key_enc")

    def test_mockbroker_rejects_bad_auth(self):
        r = requests.get(f"{API}/mockbroker/programs", timeout=TIMEOUT)
        assert r.status_code == 401
        r = requests.get(f"{API}/mockbroker/programs",
                         headers={"Authorization": "Bearer wrong"},
                         timeout=TIMEOUT)
        assert r.status_code == 401

    def test_adapter_lists_programs_over_real_http(self):
        from services.broker_gateway.broker_adapter import adapter_for
        db = _db()
        partner = _run(db.broker_partners.find_one(
            {"partner_id": "prt_rest_demo"}, {"_id": 0}))
        adapter = adapter_for(db, partner)
        progs = _run(adapter.get_pamm_programs())
        assert len(progs) >= 1 and progs[0]["program_id"]
        nav = _run(adapter.get_nav(progs[0]["program_id"]))
        assert isinstance(nav["nav"], (int, float))
        with pytest.raises(ValueError):
            _run(adapter.get_nav("mbx_does_not_exist"))

    def test_rest_demo_passes_certification_suite(self):
        s = _login()
        r = s.post(f"{API}/pamm/partners/prt_rest_demo/certify",
                   timeout=90)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["failed"] == 0, body["results"]
        assert body["certified"] is True
        assert body["score"] == 100.0


# --------------------------------------------------- MT5 Manager adapter
class TestMt5ManagerAdapter:
    def _partner(self):
        import secrets_vault
        return {"partner_id": "prt_mt5_test", "adapter": "mt5_manager",
                "mt5_config": {
                    "gateway_url": "http://mt5-gw.local",
                    "manager_login": "1001", "server": "Demo-Server",
                    "manager_password_enc": secrets_vault.encrypt(
                        "pw", associated_data=b"broker_manager_password")}}

    def _patch(self, monkeypatch, handler):
        import httpx
        from services.broker_gateway import mt5_manager_adapter as mod

        def fake_build(base_url, headers=None, timeout=10.0):
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="http://mt5-gw.local", headers=headers or {})
        monkeypatch.setattr(mod, "build_client", fake_build)
        return mod

    def test_registry_resolves_mt5_manager(self):
        from services.broker_gateway.broker_adapter import adapter_for
        from services.broker_gateway.mt5_manager_adapter import \
            Mt5ManagerAdapter
        adapter = adapter_for(None, self._partner())
        assert isinstance(adapter, Mt5ManagerAdapter)

    def test_contract_mapping(self, monkeypatch):
        import httpx

        def handler(request):
            path = request.url.path
            if path == "/auth/token":
                return httpx.Response(200, json={"token": "tok1"})
            if path == "/pamm/masters":
                return httpx.Response(200, json=[
                    {"login": 555001, "name": "Alpha", "currency": "USD",
                     "equity": 50000.0}])
            if path == "/pamm/masters/555001":
                return httpx.Response(200, json={
                    "login": 555001, "equity": 50000.0, "balance": 49000.0,
                    "currency": "USD", "investor_count": 3,
                    "trading_enabled": True})
            if path == "/pamm/masters/555001/positions":
                return httpx.Response(200, json=[
                    {"ticket": 9001, "symbol": "XAUUSD", "volume": 0.5,
                     "type": "BUY", "open_price": 2400.0, "profit": 12.5}])
            if path == "/pamm/masters/555001/close-all":
                return httpx.Response(200, json={"closed": 2})
            return httpx.Response(404, json={"detail": "not found"})
        mod = self._patch(monkeypatch, handler)
        adapter = mod.Mt5ManagerAdapter(None, self._partner())
        progs = _run(adapter.get_pamm_programs())
        assert progs[0]["program_id"] == "555001"
        assert progs[0]["master_login"] == "555001"
        nav = _run(adapter.get_nav("555001"))
        assert nav["nav"] == 50000.0
        master = _run(adapter.get_master_account("555001"))
        assert master["trading"] == "enabled"
        assert master["investor_count"] == 3
        positions = _run(adapter.get_positions("555001"))
        assert positions[0]["position_id"] == "9001"
        assert positions[0]["volume"] == 0.5
        closed = _run(adapter.close_all_positions("555001"))
        assert closed == {"program_id": "555001", "closed": 2}
        with pytest.raises(ValueError):
            _run(adapter.get_nav("999999"))

    def test_auth_failure_raises_permission_error(self, monkeypatch):
        import httpx

        def handler(request):
            return httpx.Response(403, json={"detail": "bad creds"})
        mod = self._patch(monkeypatch, handler)
        adapter = mod.Mt5ManagerAdapter(None, self._partner())
        with pytest.raises(PermissionError):
            _run(adapter.get_pamm_programs())


# ---------------------------------------------------------- position truth
class TestPositionTruth:
    def test_baseline_drift_freeze_and_ack(self):
        s = _login()
        prog = _create_program(s, f"ptr-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            # 1) first check adopts broker truth as baseline
            r = s.post(f"{API}/pamm/programs/{pid}/position-truth/check",
                       timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "baseline"
            # 2) second check — in sync
            r = s.post(f"{API}/pamm/programs/{pid}/position-truth/check",
                       timeout=TIMEOUT)
            assert r.json()["status"] == "in_sync"
            # 3) broker mints a position STOIC never expected → drift
            bpid = _run(db.pamm_programs.find_one(
                {"program_id": pid}))["broker_program_id"]
            _run(db.sandbox_broker_positions.insert_one(
                {"program_id": bpid, "position_id": "sbxpos_drift1",
                 "symbol": "XAUUSD", "volume": 1.0}))
            r = s.post(f"{API}/pamm/programs/{pid}/position-truth/check",
                       timeout=TIMEOUT)
            body = r.json()
            assert body["status"] == "drift"
            assert body["unexpected"] == ["sbxpos_drift1"]
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            assert p["op_state"] == "new_trades_paused", \
                "drift must freeze new exposure"
            inc = _run(db.pamm_incidents.find_one(
                {"program_id": pid, "type": "position_drift",
                 "status": "open"}))
            assert inc is not None
            ev = _run(db.pamm_events.find_one(
                {"type": "PositionDrift", "data.program_id": pid}))
            assert ev is not None
            # trading is blocked while frozen
            from modules.pamm.risk import trading_allowed
            allowed, reason = _run(trading_allowed(db, p))
            assert allowed is False
            # 4) GET endpoint exposes truth + open incident
            r = s.get(f"{API}/pamm/programs/{pid}/position-truth",
                      timeout=TIMEOUT)
            body = r.json()
            assert body["truth"]["status"] == "drift"
            assert body["open_incident"]["incident_id"] == inc["incident_id"]
            # 5) admin acknowledges → broker truth adopted, incident
            #    resolved, but op-state does NOT auto de-escalate
            r = s.post(
                f"{API}/pamm/programs/{pid}/position-truth/acknowledge",
                timeout=TIMEOUT)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "in_sync"
            inc2 = _run(db.pamm_incidents.find_one(
                {"incident_id": inc["incident_id"]}))
            assert inc2["status"] == "resolved"
            p2 = _run(db.pamm_programs.find_one({"program_id": pid},
                                                {"_id": 0}))
            assert p2["op_state"] == "new_trades_paused", \
                "ack must NEVER auto-resume trading"
        finally:
            _cleanup(pid)

    def test_drift_tolerance_increase_requires_dual_auth(self):
        s = _login()
        prog = _create_program(s, f"tol-{uuid.uuid4().hex[:6]}")
        pid = prog["program_id"]
        db = _db()
        try:
            # raising tolerance directly → refused (409, dual auth needed)
            r = s.put(f"{API}/pamm/programs/{pid}/drift-tolerance",
                      json={"tolerance": 0.5}, timeout=TIMEOUT)
            assert r.status_code == 409
            assert "dual authorization" in r.json()["detail"]
            # dual-auth path applies the increase
            from modules.pamm.dualauth import (create_change_request,
                                               decide_change_request)
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            req = _run(create_change_request(
                db, p, "drift_tolerance_increase", {"tolerance": 0.5},
                "admin_a", reason="test"))
            _run(decide_change_request(db, req["change_id"], True,
                                       "admin_b"))
            p2 = _run(db.pamm_programs.find_one({"program_id": pid},
                                                {"_id": 0}))
            assert p2["drift_tolerance"] == 0.5
            # lowering back down is a single safe action
            r = s.put(f"{API}/pamm/programs/{pid}/drift-tolerance",
                      json={"tolerance": 0.1}, timeout=TIMEOUT)
            assert r.status_code == 200
            assert r.json()["drift_tolerance"] == 0.1
        finally:
            _run(db.pamm_change_requests.delete_many({"program_id": pid}))
            _cleanup(pid)


# ------------------------------------------------------ partner registry
class TestPartnerRegistry:
    def test_register_partner_encrypts_and_redacts(self):
        from services.broker_gateway.pamm_api import register_partner
        db = _db()
        doc = _run(register_partner(
            db, {"name": "Test REST Broker", "adapter": "rest",
                 "rest_config": {"base_url": "https://example.com",
                                 "api_key": "sk_test_123"}}, "admin"))
        try:
            assert doc["partner_id"].startswith("prt_")
            assert "webhook_secret_enc" not in doc
            assert doc["rest_config"].get("credentials_set") is True
            assert "api_key_enc" not in doc["rest_config"]
            stored = _run(db.broker_partners.find_one(
                {"partner_id": doc["partner_id"]}, {"_id": 0}))
            import secrets_vault
            assert stored["rest_config"]["api_key_enc"] != "sk_test_123"
            assert secrets_vault.decrypt(
                stored["rest_config"]["api_key_enc"],
                associated_data=b"broker_api_key") == "sk_test_123"
        finally:
            _run(db.broker_partners.delete_many(
                {"partner_id": doc["partner_id"]}))

    def test_register_partner_validation(self):
        from services.broker_gateway.pamm_api import register_partner
        db = _db()
        with pytest.raises(ValueError):
            _run(register_partner(db, {"name": "x", "adapter": "sandbox"},
                                  "admin"))
        with pytest.raises(ValueError):
            _run(register_partner(
                db, {"name": "x", "adapter": "mt5_manager",
                     "mt5_config": {"gateway_url": "https://example.com"}},
                "admin"))

    def test_partners_endpoint_redacts_secrets(self):
        s = _login()
        r = s.get(f"{API}/pamm/partners", timeout=TIMEOUT)
        assert r.status_code == 200
        partners = r.json()["partners"]
        assert any(p["partner_id"] == "prt_rest_demo" for p in partners)
        for p in partners:
            assert "webhook_secret_enc" not in p
            for cfg in (p.get("rest_config") or {},
                        p.get("mt5_config") or {}):
                assert "api_key_enc" not in cfg
                assert "manager_password_enc" not in cfg

    def test_create_partner_requires_step_up(self):
        s = _login()
        s.headers["X-Step-Up-Bypass"] = ""  # exercise the REAL gate
        r = s.post(f"{API}/pamm/partners",
                   json={"name": "NoStepUp", "adapter": "rest",
                         "rest_config": {"base_url": "https://x",
                                         "api_key": "k"}},
                   timeout=TIMEOUT)
        assert r.status_code in (401, 403), r.text
        _run(_db().broker_partners.delete_many({"name": "NoStepUp"}))


# ------------------------------------------------------- intent routes
class TestIntentRoutes:
    def test_list_and_stats(self):
        s = _login()
        r = s.get(f"{API}/execution/intents?limit=5", timeout=TIMEOUT)
        assert r.status_code == 200
        assert "intents" in r.json()
        r = s.get(f"{API}/execution/intents/stats", timeout=TIMEOUT)
        assert r.status_code == 200
        assert "by_status" in r.json()

    def test_detail_404(self):
        s = _login()
        r = s.get(f"{API}/execution/intents/xin_DOESNOTEXIST",
                  timeout=TIMEOUT)
        assert r.status_code == 404

    def test_requires_auth(self):
        r = requests.get(f"{API}/execution/intents", timeout=TIMEOUT)
        assert r.status_code in (401, 403)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
