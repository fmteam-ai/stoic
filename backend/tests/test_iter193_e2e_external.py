"""iter-193 e2e — hits the external REACT_APP_BACKEND_URL over real HTTP.
Covers: partners listing/creation, mock broker auth, certification,
position-truth flow (baseline→in_sync→drift→ack), drift-tolerance
authorization, execution intent routes."""
import os
import sys
import uuid
import time

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


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN[0], "password": ADMIN[1]},
               timeout=TIMEOUT)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return s


# ---------------------------------------------------------- partners listing
class TestPartners:
    def test_partners_list_includes_rest_demo_and_redacts_secrets(self, admin_session):
        r = admin_session.get(f"{API}/pamm/partners", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        data = r.json()
        partners = data.get("partners") if isinstance(data, dict) else data
        assert isinstance(partners, list), data
        ids = [p.get("partner_id") for p in partners]
        assert "prt_rest_demo" in ids, ids
        rest_demo = next(p for p in partners if p["partner_id"] == "prt_rest_demo")
        # No *_enc secret fields must leak (top-level or nested config)
        def _scan(d, path="root"):
            if isinstance(d, dict):
                for k, v in d.items():
                    assert not k.endswith("_enc"), f"leaked secret field: {path}.{k}"
                    _scan(v, f"{path}.{k}")
        _scan(rest_demo)
        rc = rest_demo.get("rest_config") or {}
        # api_key must be redacted; credentials_set flag expected
        assert "api_key" not in rc, f"raw api_key leaked: {rc}"
        assert rc.get("credentials_set") is True, f"missing credentials_set: {rc}"

    def test_create_partner_without_stepup_rejected(self, admin_session):
        # Explicitly strip the step-up bypass header for this call
        headers = {"X-Step-Up-Bypass": ""}
        payload = {"name": f"TEST_np_{uuid.uuid4().hex[:6]}",
                   "adapter": "rest",
                   "rest_config": {"base_url": "http://x", "api_key": "k"}}
        r = admin_session.post(f"{API}/pamm/partners", json=payload,
                               headers=headers, timeout=TIMEOUT)
        assert r.status_code in (401, 403), f"expected 401/403 got {r.status_code} {r.text}"

    def test_create_partner_invalid_kind_rejected(self, admin_session):
        payload = {"name": f"TEST_np_{uuid.uuid4().hex[:6]}",
                   "adapter": "unknown_adapter",
                   "rest_config": {"base_url": "http://x", "api_key": "k"}}
        r = admin_session.post(f"{API}/pamm/partners", json=payload, timeout=TIMEOUT)
        assert r.status_code == 400, f"expected 400 got {r.status_code} {r.text}"

    def test_create_rest_partner_with_stepup_ok(self, admin_session):
        name = f"TEST_np_{uuid.uuid4().hex[:6]}"
        payload = {"name": name, "adapter": "rest",
                   "rest_config": {"base_url": f"{API}/mockbroker",
                                   "api_key": "mock-broker-secret-key-do-not-use-in-prod"}}
        r = admin_session.post(f"{API}/pamm/partners", json=payload, timeout=TIMEOUT)
        assert r.status_code in (200, 201), f"{r.status_code} {r.text}"
        body = r.json()
        # secrets must not appear anywhere
        import json as _json
        blob = _json.dumps(body).lower()
        assert "mock-broker-secret-key" not in blob, "raw api key leaked in response"
        assert "_enc" not in blob, "encrypted field name leaked in response"
        # cleanup
        try:
            _run(_db().broker_partners.delete_one({"partner_id": body.get("partner_id")}))
        except Exception:
            pass


# ---------------------------------------------------------- mockbroker auth
class TestMockBrokerAuth:
    def test_mockbroker_rejects_no_auth(self):
        r = requests.get(f"{API}/mockbroker/programs", timeout=TIMEOUT)
        assert r.status_code == 401, f"{r.status_code} {r.text}"

    def test_mockbroker_rejects_bad_auth(self):
        r = requests.get(f"{API}/mockbroker/programs",
                         headers={"Authorization": "Bearer wrong-key"},
                         timeout=TIMEOUT)
        assert r.status_code == 401, f"{r.status_code} {r.text}"


# ---------------------------------------------------------- certification
class TestCertification:
    def test_rest_demo_certifies_over_real_http(self, admin_session):
        r = admin_session.post(f"{API}/pamm/partners/prt_rest_demo/certify",
                               timeout=60)
        assert r.status_code == 200, r.text
        data = r.json()
        assert data.get("certified") is True, data
        assert data.get("failed", -1) == 0, data
        assert data.get("score") == 100, data


# ---------------------------------------------------------- position truth
class TestPositionTruth:
    @pytest.fixture
    def program(self, admin_session):
        name = f"TEST_e2e_ptruth_{uuid.uuid4().hex[:8]}"
        r = admin_session.post(f"{API}/pamm/programs", json={"name": name}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        p = r.json()
        pid = p["program_id"]
        yield p
        # cleanup
        db = _db()
        bpid = (_run(db.pamm_programs.find_one({"program_id": pid})) or {}).get("broker_program_id")
        for c in ("pamm_programs", "pamm_master_accounts", "pamm_allocations",
                  "pamm_nav_snapshots", "pamm_reconciliation", "pamm_audit",
                  "pamm_incidents", "pamm_position_truth",
                  "pamm_expected_positions", "execution_intents"):
            _run(getattr(db, c).delete_many({"program_id": pid}))
        _run(db.pamm_events.delete_many({"data.program_id": pid}))
        if bpid:
            for c in ("sandbox_broker_programs", "sandbox_broker_investors",
                      "sandbox_broker_allocations", "sandbox_broker_positions"):
                _run(getattr(db, c).delete_many({"program_id": bpid}))

    def test_full_position_truth_flow(self, admin_session, program):
        pid = program["program_id"]
        db = _db()

        # First check: baseline OR in_sync (background sweep may have baselined already)
        r = admin_session.post(f"{API}/pamm/programs/{pid}/position-truth/check", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        s1 = r.json().get("status")
        assert s1 in ("baseline", "in_sync"), r.json()

        # Second check: in_sync
        r = admin_session.post(f"{API}/pamm/programs/{pid}/position-truth/check", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        assert r.json().get("status") == "in_sync", r.json()

        # Inject fake broker position → drift
        prog_doc = _run(db.pamm_programs.find_one({"program_id": pid}))
        bpid = prog_doc.get("broker_program_id")
        assert bpid, prog_doc
        _run(db.sandbox_broker_positions.insert_one({
            "program_id": bpid,
            "position_id": "sbxpos_e2e_drift1",
            "symbol": "EURUSD",
            "side": "buy",
            "volume": 1.0,
            "opened_at": time.time(),
        }))

        r = admin_session.post(f"{API}/pamm/programs/{pid}/position-truth/check", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("status") == "drift", body

        # program op_state → new_trades_paused
        prog_after = _run(db.pamm_programs.find_one({"program_id": pid}))
        assert prog_after.get("op_state") == "new_trades_paused", prog_after.get("op_state")

        # Open incident
        inc = _run(db.pamm_incidents.find_one({"program_id": pid, "type": "position_drift",
                                                "status": "open"}))
        assert inc, "expected open position_drift incident"

        # PositionDrift event
        ev = _run(db.pamm_events.find_one({"program_id": pid, "type": "PositionDrift"})) or \
             _run(db.pamm_events.find_one({"data.program_id": pid, "type": "PositionDrift"}))
        assert ev, "expected PositionDrift event"

        # GET returns truth + open_incident
        r = admin_session.get(f"{API}/pamm/programs/{pid}/position-truth", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("open_incident"), body
        assert body.get("truth") or body.get("status"), body

        # Acknowledge → in_sync, incident resolved, op_state STAYS new_trades_paused
        r = admin_session.post(f"{API}/pamm/programs/{pid}/position-truth/acknowledge",
                               timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        ack = r.json()
        assert ack.get("status") == "in_sync", ack

        inc2 = _run(db.pamm_incidents.find_one({"program_id": pid, "type": "position_drift",
                                                 "status": "open"}))
        assert inc2 is None, f"incident should be resolved, still open: {inc2}"

        prog_final = _run(db.pamm_programs.find_one({"program_id": pid}))
        assert prog_final.get("op_state") == "new_trades_paused", \
            f"ack must NOT auto-resume, got op_state={prog_final.get('op_state')}"


# ---------------------------------------------------------- drift tolerance
class TestDriftTolerance:
    @pytest.fixture
    def program(self, admin_session):
        name = f"TEST_e2e_drifttol_{uuid.uuid4().hex[:8]}"
        r = admin_session.post(f"{API}/pamm/programs", json={"name": name}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        p = r.json()
        yield p
        _run(_db().pamm_programs.delete_many({"program_id": p["program_id"]}))

    def test_increase_requires_dual_auth(self, admin_session, program):
        pid = program["program_id"]
        # current tolerance is default; try a big increase
        r = admin_session.put(f"{API}/pamm/programs/{pid}/drift-tolerance",
                              json={"tolerance": 999.0}, timeout=TIMEOUT)
        assert r.status_code == 409, f"{r.status_code} {r.text}"
        assert "dual" in r.text.lower() or "authoriz" in r.text.lower(), r.text

    def test_decrease_ok(self, admin_session, program):
        pid = program["program_id"]
        r = admin_session.put(f"{API}/pamm/programs/{pid}/drift-tolerance",
                              json={"tolerance": 0.0}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text


# ---------------------------------------------------------- intent routes
class TestIntentRoutes:
    def test_list_and_stats(self, admin_session):
        r = admin_session.get(f"{API}/execution/intents", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        r = admin_session.get(f"{API}/execution/intents/stats", timeout=TIMEOUT)
        assert r.status_code == 200, r.text

    def test_detail_404(self, admin_session):
        r = admin_session.get(f"{API}/execution/intents/xin_doesnotexist_zzz", timeout=TIMEOUT)
        assert r.status_code == 404, r.text

    def test_unauthenticated(self):
        s = requests.Session()
        r = s.get(f"{API}/execution/intents", timeout=TIMEOUT)
        assert r.status_code in (401, 403), r.status_code


# ---------------------------------------------------------- regression
class TestRegression:
    def test_sweep_status(self, admin_session):
        r = admin_session.get(f"{API}/pamm/sweep-status", timeout=TIMEOUT)
        assert r.status_code == 200, r.text


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
