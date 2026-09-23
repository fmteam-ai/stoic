from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-194 — security audit fixes for the v55 surface:
SEC-001 tenant-scoped execution-intent reads, SEC-002 SSRF guards on
broker partner URLs + sanitized adapter errors, SEC-003 mock broker
hardening (constant-time auth, insert caps, prod gating)."""
import os
import sys
import uuid

import pytest
import requests

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
ADMIN = (ADMIN_EMAIL, ADMIN_PASSWORD)
TIMEOUT = 30


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _login(email=ADMIN[0], password=ADMIN[1]):
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": password},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


# ------------------------------------------------- SEC-001 intent scoping
class TestIntentReadScoping:
    def test_manager_sees_only_own_intents(self):
        from execution_intents import create_intent
        db = _db()
        admin = _login()
        email = f"mgr-{uuid.uuid4().hex[:8]}@mailinator.com"
        pw = "Vx7#Qm2pL9wTzK4e"
        r = requests.post(f"{API}/auth/register",
                          json={"terms_agreed": True, "email": email,
                                "password": pw, "name": "Sec Mgr"},
                          timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        _run(db.users.update_one({"email": email},
                                 {"$set": {"email_verified": True}}))
        mgr = _login(email, pw)
        me = mgr.get(f"{API}/auth/me", timeout=TIMEOUT).json()
        uid = me.get("id") or me.get("user", {}).get("id")
        assert uid
        r = admin.post(f"{API}/pamm/managers",
                       json={"user_id": uid, "grant": True},
                       timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        foreign = _run(create_intent(db, source="test", kind="open_trade",
                                     program_id="pgm_foreign_sec194",
                                     actor="someone_else"))
        own = _run(create_intent(db, source="test", kind="open_trade",
                                 actor=uid))
        try:
            body = mgr.get(f"{API}/execution/intents?limit=200",
                           timeout=TIMEOUT).json()
            ids = {i["intent_id"] for i in body["intents"]}
            assert foreign["intent_id"] not in ids, \
                "manager can read foreign intents (SEC-001 regression)"
            assert own["intent_id"] in ids
            r = mgr.get(f"{API}/execution/intents/{foreign['intent_id']}",
                        timeout=TIMEOUT)
            assert r.status_code == 404
            r = mgr.get(f"{API}/execution/intents/{own['intent_id']}",
                        timeout=TIMEOUT)
            assert r.status_code == 200
            # admin remains unrestricted
            body = admin.get(f"{API}/execution/intents?limit=200",
                             timeout=TIMEOUT).json()
            ids = {i["intent_id"] for i in body["intents"]}
            assert foreign["intent_id"] in ids
        finally:
            _run(db.execution_intents.delete_many(
                {"intent_id": {"$in": [foreign["intent_id"],
                                       own["intent_id"]]}}))
            _run(db.users.delete_many({"email": email}))

    def test_non_manager_rejected(self):
        email = f"usr-{uuid.uuid4().hex[:8]}@mailinator.com"
        pw = "Vx7#Qm2pL9wTzK4e"
        r = requests.post(f"{API}/auth/register",
                          json={"terms_agreed": True, "email": email,
                                "password": pw, "name": "Plain"},
                          timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        _run(_db().users.update_one({"email": email},
                                    {"$set": {"email_verified": True}}))
        s = _login(email, pw)
        try:
            r = s.get(f"{API}/execution/intents", timeout=TIMEOUT)
            assert r.status_code == 403
        finally:
            _run(_db().users.delete_many({"email": email}))


# ---------------------------------------------------- SEC-002 SSRF guard
class TestBrokerUrlSsrfGuard:
    def _reject(self, payload):
        from services.broker_gateway.pamm_api import register_partner
        with pytest.raises(ValueError):
            _run(register_partner(_db(), payload, "admin"))

    def test_private_and_internal_urls_refused(self):
        for url in ("http://localhost:8001/api/mockbroker",
                    "http://127.0.0.1:8001", "http://10.0.0.5/x",
                    "http://169.254.169.254/latest/meta-data",
                    "http://192.168.1.1", "ftp://example.com",
                    "http://0.0.0.0"):
            self._reject({"name": "evil", "adapter": "rest",
                          "rest_config": {"base_url": url,
                                          "api_key": "k"}})
        self._reject({"name": "evil", "adapter": "mt5_manager",
                      "mt5_config": {"gateway_url": "http://127.0.0.1:80",
                                     "manager_login": "1",
                                     "server": "x",
                                     "manager_password": "p"}})

    def test_endpoint_overrides_must_be_relative(self):
        self._reject({"name": "evil", "adapter": "rest",
                      "rest_config": {"base_url": "https://example.com",
                                      "api_key": "k",
                                      "endpoints": {
                                          "nav": "GET http://evil.com/x"}}})
        self._reject({"name": "evil", "adapter": "rest",
                      "rest_config": {"base_url": "https://example.com",
                                      "api_key": "k",
                                      "endpoints": {"nav": "TRACE /x"}}})

    def test_api_endpoint_returns_400_for_internal_url(self):
        s = _login()
        r = s.post(f"{API}/pamm/partners",
                   json={"name": "evil", "adapter": "rest",
                         "rest_config": {"base_url": "http://127.0.0.1:8001",
                                         "api_key": "k"}},
                   timeout=TIMEOUT)
        assert r.status_code == 400
        assert "private" in r.json()["detail"]

    def test_adapter_errors_do_not_echo_response_body(self, monkeypatch):
        import httpx
        import secrets_vault
        from services.broker_gateway import rest_adapter as mod

        def handler(request):
            return httpx.Response(500, text="INTERNAL-SECRET-TOKEN-XYZ")

        def fake_build(base_url, headers, timeout):
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url="https://broker.example", headers=headers)
        monkeypatch.setattr(mod, "build_client", fake_build)
        adapter = mod.RestBrokerAdapter(None, {
            "partner_id": "prt_x", "adapter": "rest",
            "rest_config": {"base_url": "https://broker.example",
                            "api_key_enc": secrets_vault.encrypt(
                                "k", associated_data=b"broker_api_key")}})
        with pytest.raises(ValueError) as exc:
            _run(adapter.get_pamm_programs())
        assert "INTERNAL-SECRET-TOKEN-XYZ" not in str(exc.value)
        assert "500" in str(exc.value)


# --------------------------------------------- SEC-003 mock broker limits
class TestMockBrokerHardening:
    def _key(self):
        import secrets_vault
        p = _run(_db().broker_partners.find_one(
            {"partner_id": "prt_rest_demo"}, {"_id": 0}))
        return secrets_vault.decrypt(p["rest_config"]["api_key_enc"],
                                     associated_data=b"broker_api_key")

    def test_position_insert_cap(self):
        db = _db()
        key = self._key()
        h = {"Authorization": f"Bearer {key}"}
        r = requests.post(f"{API}/mockbroker/programs",
                          json={"name": f"cap-{uuid.uuid4().hex[:6]}"},
                          headers=h, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        pid = r.json()["program_id"]
        try:
            _run(db.mockbroker_positions.insert_many(
                [{"position_id": f"cap_{i}", "program_id": pid,
                  "symbol": "X", "volume": 0.1} for i in range(500)]))
            r = requests.post(f"{API}/mockbroker/programs/{pid}/positions",
                              json={"symbol": "XAUUSD"}, headers=h,
                              timeout=TIMEOUT)
            assert r.status_code == 429
        finally:
            _run(db.mockbroker_positions.delete_many({"program_id": pid}))
            _run(db.mockbroker_programs.delete_many({"program_id": pid}))

    def test_auth_still_fails_closed(self):
        for hdr in ({}, {"Authorization": "Bearer nope"},
                    {"Authorization": "Basic xyz"}):
            r = requests.get(f"{API}/mockbroker/programs", headers=hdr,
                             timeout=TIMEOUT)
            assert r.status_code == 401


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
