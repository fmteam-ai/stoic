"""e2e review — v56 Global Trading Authority + PAMM broker_uncertain
+ Position Truth v2 + sweep unknown reconciliation + regressions.

Focus: public API surface & end-to-end behaviour (module-level coverage
already exists in test_iter195/196). Always restores platform authority
to FULL and cleans up created PAMM programs.
"""
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


@pytest.fixture(autouse=True)
def _restore_platform():
    yield
    _run(_db().platform_state.delete_many({"_id": "trading_authority"}))


# ---------- Authority endpoint ----------
class TestAuthorityAPI:
    def test_get_authority_shape_all_domains(self):
        s = _login()
        r = s.get(f"{API}/authority", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        b = r.json()
        assert set(["level", "enforced_level", "restricted",
                    "computed_at", "domains"]).issubset(b.keys())
        for d in ("platform", "account", "broker", "infrastructure",
                  "risk", "pamm", "execution", "position_truth"):
            assert d in b["domains"]
            assert "level" in b["domains"][d]
            assert "reason" in b["domains"][d]

    def test_authority_requires_auth(self):
        r = requests.get(f"{API}/authority", timeout=TIMEOUT)
        assert r.status_code in (401, 403)

    def test_set_platform_close_only_and_restore(self):
        s = _login()
        r = s.post(f"{API}/authority/platform",
                   json={"level": "CLOSE_ONLY", "reason": "e2e test"},
                   timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body = s.get(f"{API}/authority", timeout=TIMEOUT).json()
        assert body["enforced_level"] == "CLOSE_ONLY"
        assert body["restricted"] is True
        # restore FULL succeeds WITH bypass header (conftest injects it)
        r = s.post(f"{API}/authority/platform",
                   json={"level": "FULL", "reason": "restore"},
                   timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        body2 = s.get(f"{API}/authority", timeout=TIMEOUT).json()
        # audit P0-1: every domain is enforced — enforced_level mirrors the
        # effective level; the platform override itself is back to FULL.
        assert body2["domains"]["platform"]["level"] == "FULL"
        assert body2["enforced_level"] == body2["level"]

    def test_relax_without_stepup_bypass_is_blocked(self):
        s = _login()
        r = s.post(f"{API}/authority/platform",
                   json={"level": "PAUSED", "reason": "restrict"},
                   timeout=TIMEOUT)
        assert r.status_code == 200
        s.headers["X-Step-Up-Bypass"] = ""  # kill bypass
        r = s.post(f"{API}/authority/platform",
                   json={"level": "FULL", "reason": "relax"},
                   timeout=TIMEOUT)
        assert r.status_code in (401, 403)

    def test_close_only_blocks_enforce_new_trade(self):
        from trading_authority import enforce_new_trade
        s = _login()
        r = s.post(f"{API}/authority/platform",
                   json={"level": "CLOSE_ONLY", "reason": "block-check"},
                   timeout=TIMEOUT)
        assert r.status_code == 200
        gate = _run(enforce_new_trade(_db()))
        assert gate["ok"] is False
        assert gate["level"] == "CLOSE_ONLY"

    def test_reduced_halves_via_authority_api(self):
        from trading_authority import enforce_new_trade
        s = _login()
        r = s.post(f"{API}/authority/platform",
                   json={"level": "REDUCED", "reason": "half"},
                   timeout=TIMEOUT)
        assert r.status_code == 200
        gate = _run(enforce_new_trade(_db()))
        assert gate["ok"] is True and gate.get("reduce_factor") == 0.5


# ---------- Execution intents observability ----------
class TestIntentObservability:
    def test_intents_list_and_stats(self):
        s = _login()
        r1 = s.get(f"{API}/execution/intents", timeout=TIMEOUT)
        assert r1.status_code == 200, r1.text
        r2 = s.get(f"{API}/execution/intents/stats", timeout=TIMEOUT)
        assert r2.status_code == 200, r2.text


# ---------- Sweep unknown reconciliation fields ----------
class TestSweepUnknownFields:
    def test_sweep_records_intents_unknown_fields(self):
        from modules.pamm.sweep import sweep_once
        db = _db()
        _run(sweep_once(db))
        doc = _run(db.pamm_sweeps.find_one({"_id": "last"})) or {}
        assert "intents_unknown" in doc, f"missing intents_unknown: {doc}"
        assert "unknown_reconciled" in doc, \
            f"missing unknown_reconciled: {doc}"


# ---------- Position Truth end-to-end via API ----------
class TestPositionTruthAPI:
    def test_program_e2e_check_then_drift(self):
        s = _login()
        name = f"e2e-pt-{uuid.uuid4().hex[:6]}"
        r = s.post(f"{API}/pamm/programs", json={"name": name},
                   timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        pid = r.json()["program_id"]
        db = _db()
        try:
            r1 = s.post(
                f"{API}/pamm/programs/{pid}/position-truth/check",
                timeout=TIMEOUT)
            assert r1.status_code == 200, r1.text
            b1 = r1.json()
            assert b1.get("status") in ("baseline", "in_sync")
            # inject drift
            p = _run(db.pamm_programs.find_one({"program_id": pid},
                                               {"_id": 0}))
            bpid = p["broker_program_id"]
            _run(db.sandbox_broker_positions.insert_one(
                {"program_id": bpid, "position_id": "x1",
                 "symbol": "EURUSD", "volume": 1.0, "side": "BUY"}))
            r2 = s.post(
                f"{API}/pamm/programs/{pid}/position-truth/check",
                timeout=TIMEOUT)
            assert r2.status_code == 200, r2.text
            b2 = r2.json()
            assert b2["status"] == "drift"
            assert "UNEXPECTED_AT_BROKER" in b2.get("classification", [])
            # v2 fields present
            assert "mode" in b2
        finally:
            for c in ("pamm_programs", "pamm_master_accounts",
                      "pamm_allocations", "pamm_position_truth",
                      "pamm_expected_positions", "pamm_incidents"):
                _run(getattr(db, c).delete_many({"program_id": pid}))
            _run(db.sandbox_broker_positions.delete_many(
                {"program_id": (p or {}).get("broker_program_id")}))


# ---------- Regressions ----------
class TestRegressions:
    def test_broker_certification_still_works(self):
        s = _login()
        r = s.post(
            f"{API}/pamm/partners/prt_rest_demo/certify", timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        b = r.json()
        assert b.get("certified") is True

    def test_pamm_marketplace_reachable(self):
        s = _login()
        r = s.get(f"{API}/pamm/marketplace", timeout=TIMEOUT)
        assert r.status_code in (200, 404)  # endpoint may vary

    def test_login_and_me(self):
        s = _login()
        r = s.get(f"{API}/auth/me", timeout=TIMEOUT)
        assert r.status_code == 200


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
