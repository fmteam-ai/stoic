from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""Iter-82 verification — Phases B/D/E backend (metrics gauges, ops alerts,
validation ledger, stage machine, bot start regression). Runs against the
public preview URL using METRICS_TOKEN + STEP_UP_BYPASS from backend/.env.

Cleanup: restores deployment_stage to internal_shadow and deletes any
TEST-prefixed validation_evidence rows.
"""
import os
import re
import uuid
import pytest
import requests

from live_target import require_live_base_url
BASE_URL = require_live_base_url()


def _env(name):
    import pathlib
    env_path = pathlib.Path(__file__).resolve().parents[1] / ".env"
    with open(env_path) as f:
        for line in f:
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"')
    return ""


METRICS_TOKEN = _env("METRICS_TOKEN")
STEP_UP_BYPASS = _env("STEP_UP_BYPASS_TOKEN")


@pytest.fixture(scope="session")
def metrics_headers():
    return {"X-Metrics-Token": METRICS_TOKEN}


@pytest.fixture(scope="session")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    return s


# ---------- Phase B: metrics + ops alerts ----------
class TestMetrics:
    def test_metrics_token_gate(self):
        # no token → 403
        r = requests.get(f"{BASE_URL}/api/metrics")
        assert r.status_code == 403
        # bad token → 403
        r = requests.get(f"{BASE_URL}/api/metrics",
                         headers={"X-Metrics-Token": "wrong"})
        assert r.status_code == 403

    def test_metrics_gauges_present(self, metrics_headers):
        r = requests.get(f"{BASE_URL}/api/metrics", headers=metrics_headers)
        assert r.status_code == 200
        body = r.text
        # existing gauges (regression)
        assert "stoic_mongo_latency_ms" in body
        assert re.search(r'stoic_trades\{status="open"\}', body)
        assert "stoic_outbox_pending" in body
        # new/req gauges
        assert re.search(r'stoic_alerts_unacked\{severity="critical"\}', body)
        assert re.search(r'stoic_alerts_unacked\{severity="warning"\}', body)
        assert re.search(r'stoic_alerts_unacked\{severity="info"\}', body)
        assert "stoic_protection_latency_samples" in body


class TestOpsAlerts:
    def test_alerts_unauthenticated_403(self):
        r = requests.get(f"{BASE_URL}/api/ops/alerts")
        assert r.status_code == 403

    def test_alerts_via_metrics_token(self, metrics_headers):
        r = requests.get(f"{BASE_URL}/api/ops/alerts",
                         headers=metrics_headers)
        assert r.status_code == 200
        data = r.json()
        assert "alerts" in data and "unacked" in data
        assert isinstance(data["alerts"], list)
        assert isinstance(data["unacked"], int)

    def test_alerts_via_admin_session(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/ops/alerts")
        assert r.status_code == 200
        data = r.json()
        assert "alerts" in data
        # ids should be strings (ObjectId serialised out)
        for a in data["alerts"][:5]:
            assert isinstance(a["id"], str)
            assert "_id" not in a
            assert a.get("severity") in ("critical", "warning", "info")

    def test_ack_bogus_id_returns_404(self, metrics_headers):
        r = requests.post(
            f"{BASE_URL}/api/ops/alerts/notavalidobjectid/ack",
            headers=metrics_headers)
        assert r.status_code == 404

    def test_ack_valid_but_missing_returns_404(self, metrics_headers):
        r = requests.post(
            f"{BASE_URL}/api/ops/alerts/000000000000000000000000/ack",
            headers=metrics_headers)
        assert r.status_code == 404


# ---------- Phase D: validation ledger ----------
class TestValidationLedger:
    _created_ids = []

    def test_get_validation_returns_12_scenarios_both_modes(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/ops/validation")
        assert r.status_code == 200
        d = r.json()
        assert d.get("required_modes") == ["netting", "hedging"]
        assert len(d["scenarios"]) == 12
        for s in d["scenarios"]:
            assert set(s["modes"].keys()) == {"netting", "hedging"}

    def test_record_evidence_creates_row(self, admin_session):
        r = admin_session.post(
            f"{BASE_URL}/api/ops/validation/restart_recovery",
            json={"status": "pass", "account_mode": "netting",
                  "notes": f"TEST iter82 {uuid.uuid4()}"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True and "id" in data
        TestValidationLedger._created_ids.append(data["id"])

        # confirm it's returned by GET
        r2 = admin_session.get(f"{BASE_URL}/api/ops/validation")
        for s in r2.json()["scenarios"]:
            if s["scenario"] == "restart_recovery":
                assert s["modes"]["netting"] is not None
                assert s["modes"]["netting"]["status"] == "pass"
                break

    def test_unknown_scenario_404(self, admin_session):
        r = admin_session.post(
            f"{BASE_URL}/api/ops/validation/bogus_scenario",
            json={"status": "pass", "account_mode": "netting",
                  "notes": "TEST invalid"})
        assert r.status_code == 404

    def test_invalid_status_422(self, admin_session):
        r = admin_session.post(
            f"{BASE_URL}/api/ops/validation/restart_recovery",
            json={"status": "wat", "account_mode": "netting",
                  "notes": "TEST invalid"})
        assert r.status_code == 422

    def test_invalid_mode_422(self, admin_session):
        r = admin_session.post(
            f"{BASE_URL}/api/ops/validation/restart_recovery",
            json={"status": "pass", "account_mode": "wat",
                  "notes": "TEST invalid"})
        assert r.status_code == 422

    def test_unauthenticated_403(self):
        r = requests.post(f"{BASE_URL}/api/ops/validation/restart_recovery",
                          json={"status": "pass", "account_mode": "netting",
                                "notes": "TEST unauth"})
        assert r.status_code == 403

    def test_cleanup_test_evidence(self):
        """Delete evidence created in earlier tests via mongo (best-effort)."""
        try:
            from pymongo import MongoClient
            db_name = _env("DB_NAME")
            client = MongoClient("mongodb://localhost:27017")
            db = client[db_name]
            from bson import ObjectId
            for oid in TestValidationLedger._created_ids:
                db.validation_evidence.delete_one({"_id": ObjectId(oid)})
            # Also remove any TEST-prefixed evidence
            db.validation_evidence.delete_many(
                {"notes": {"$regex": "^TEST"}})
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"cleanup skipped (local mongo not available): {e}")


# ---------- Phase E: stage machine ----------
class TestStageMachine:
    def test_get_stage_shape(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/ops/stage")
        assert r.status_code == 200
        d = r.json()
        assert d["stages"] == ["internal_shadow", "demo_broker",
                               "small_live", "larger_live", "production"]
        assert d["stage"] in d["stages"]
        assert isinstance(d["promotion_criteria"], list)
        assert "can_promote" in d
        assert "enforcement" in d

    def test_promote_without_force_409_when_criteria_fail(self, admin_session):
        # snapshot current stage; if already able to promote, skip
        pre = admin_session.get(f"{BASE_URL}/api/ops/stage").json()
        if pre["can_promote"]:
            pytest.skip("criteria already pass — can't verify block path")
        r = admin_session.post(f"{BASE_URL}/api/ops/stage/promote", json={})
        assert r.status_code == 409
        d = r.json()
        # Detail may be nested under "detail"
        detail = d.get("detail") if isinstance(d.get("detail"), dict) else d
        assert "failed" in detail or "criteria" in str(d).lower()

    def test_promote_force_without_reason_422(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/ops/stage/promote",
                               json={"force": True})
        # Only fires if criteria fail; if criteria pass this would succeed 200
        pre = admin_session.get(f"{BASE_URL}/api/ops/stage").json()
        if pre["can_promote"]:
            pytest.skip("criteria pass — force-without-reason path skipped")
        assert r.status_code == 422

    def test_promote_and_demote_roundtrip(self, admin_session):
        pre = admin_session.get(f"{BASE_URL}/api/ops/stage").json()
        stage_before = pre["stage"]
        # force promote with reason
        r = admin_session.post(
            f"{BASE_URL}/api/ops/stage/promote",
            json={"force": True, "reason": "iter82 test — will be reverted"})
        assert r.status_code == 200, r.text
        promoted = r.json()
        assert promoted["stage"] != stage_before
        assert promoted["ok"] is True

        # demote back
        r2 = admin_session.post(
            f"{BASE_URL}/api/ops/stage/demote",
            json={"reason": "iter82 test cleanup"})
        assert r2.status_code == 200, r2.text
        assert r2.json()["stage"] == stage_before

    def test_demote_without_reason_422(self, admin_session):
        r = admin_session.post(f"{BASE_URL}/api/ops/stage/demote", json={})
        assert r.status_code == 422

    def test_unauth_403(self):
        r = requests.get(f"{BASE_URL}/api/ops/stage")
        assert r.status_code == 403

    def test_restore_stage(self, admin_session):
        """Force stage back to internal_shadow, whatever it currently is."""
        for _ in range(6):
            cur = admin_session.get(f"{BASE_URL}/api/ops/stage").json()
            if cur["stage"] == "internal_shadow":
                break
            admin_session.post(f"{BASE_URL}/api/ops/stage/demote",
                               json={"reason": "iter82 restore"})
        final = admin_session.get(f"{BASE_URL}/api/ops/stage").json()
        assert final["stage"] == "internal_shadow"


# ---------- Regression: bot start unaffected by stage gate ----------
class TestBotRegression:
    def test_bot_status_admin_ok(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/bot/status")
        # Should be a 200 or a well-formed response, not 500
        assert r.status_code in (200, 404), r.text

    def test_auth_me_still_works(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/auth/me")
        assert r.status_code == 200
        assert r.json().get("email") == ADMIN_EMAIL


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
