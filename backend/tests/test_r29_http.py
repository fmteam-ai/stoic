"""HTTP-level verification of r29 audit remediation.

Extends test_r28_http.py — focuses on the NEW r29 contract pieces:
 - P1-02 DEMO proof-gated attestation + audited admin override
 - account-environments listing exposes `proof`, `verifier`, `attestation_state`
 - P2-01 verified-performance share refused while provenance is stale
 - P2-04 vault rewrap endpoint + release-readiness `secrets_rewrap` check
 - subscription/plans + account-environments integration contract

Runs against REACT_APP_BACKEND_URL. Uses admin cookie auth.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.http
import requests
from bson import ObjectId
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

FRONTEND_ENV = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend", ".env")
BASE_URL = None
with open(FRONTEND_ENV) as f:
    for ln in f:
        if ln.startswith("REACT_APP_BACKEND_URL="):
            BASE_URL = ln.split("=", 1)[1].strip().strip('"').rstrip("/")
            break
assert BASE_URL, "REACT_APP_BACKEND_URL missing"

ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL") or "admin@trading.bot"
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD")
METRICS_TOKEN = os.environ.get("METRICS_TOKEN")


@pytest.fixture(scope="module")
def admin_session():
    if not ADMIN_PASSWORD:
        pytest.skip("TEST_ADMIN_PASSWORD not in env")
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=20)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    assert "access_token" in s.cookies
    return s


@pytest.fixture(scope="module")
def db():
    mclient = MongoClient(os.environ["MONGO_URL"])
    yield mclient[os.environ["DB_NAME"]]
    mclient.close()


def _acc(uid, suffix, **over):
    base = {"user_id": uid, "label": f"r29http-{suffix}", "mode": "live",
            "account_type": "demo", "broker": "VT Markets",
            "server": "VTMarkets-Demo", "broker_server": "VTMarkets-Demo",
            "account_number": f"{suffix}-{uid[-6:]}",
            "bridge_token": f"r29http-{suffix}-{uid}",
            "ea_version": "1.57", "creds_version": 0,
            "broker_account_id_reported": f"{suffix}-{uid[-6:]}",
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "ea_identity": {"installation_id": f"inst-{suffix}-{uid[-6:]}",
                            "authoritative": True, "broker_server": "VTMarkets-Demo"}}
    base.update(over)
    if "broker_account_id_reported" not in over and "account_number" in over:
        base["broker_account_id_reported"] = over["account_number"]
    return base


# ─── P1-02 attestation: demo_proof_missing / override flow ───
def test_attest_demo_proof_missing_and_override_flow(admin_session, db):
    uid = str(ObjectId())
    stale_hb = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    stale_doc = _acc(uid, "stale", last_heartbeat=stale_hb)
    ecn_doc = _acc(uid, "ecn", server="RoboForex-ECN", broker_server="RoboForex-ECN",
                   ea_identity={"installation_id": f"inst-ecn-{uid[-6:]}",
                                "authoritative": True, "broker_server": "RoboForex-ECN"})
    good_doc = _acc(uid, "good")
    stale_id = db.accounts.insert_one(stale_doc).inserted_id
    ecn_id = db.accounts.insert_one(ecn_doc).inserted_id
    good_id = db.accounts.insert_one(good_doc).inserted_id
    checked = {"stale": False, "ecn_no_override": False, "short_reason": False,
               "override_good": False, "proven_demo": False}

    def _call(acc_id, body):
        return admin_session.post(f"{BASE_URL}/api/admin/account-environments/{acc_id}",
                                  json=body, timeout=20)
    try:
        # (1) stale heartbeat → 409 demo_proof_missing with failed_checks
        r = _call(stale_id, {"environment": "DEMO", "password": ADMIN_PASSWORD,
                             "reason": "r29 http proof test"})
        if r.status_code != 429:
            assert r.status_code == 409, f"stale→expected 409 got {r.status_code} {r.text[:300]}"
            body = r.json().get("detail", {})
            assert body.get("code") == "demo_proof_missing"
            assert "heartbeat_fresh" in body.get("failed_checks", []), body
            checked["stale"] = True

        # (2) demo-named-server fails → 409 server_not_demo_named (no override)
        r = _call(ecn_id, {"environment": "DEMO", "password": ADMIN_PASSWORD, "reason": "no override"})
        if r.status_code != 429:
            assert r.status_code == 409, f"ecn→expected 409 got {r.status_code} {r.text[:300]}"
            assert r.json().get("detail", {}).get("code") == "server_not_demo_named"
            checked["ecn_no_override"] = True

        # (3) override=True but reason<10 chars → 422 override_reason_required
        r = _call(ecn_id, {"environment": "DEMO", "password": ADMIN_PASSWORD,
                           "override": True, "reason": "short"})
        if r.status_code != 429:
            assert r.status_code == 422, f"short-reason→expected 422 got {r.status_code} {r.text[:300]}"
            assert r.json().get("detail", {}).get("code") == "override_reason_required"
            checked["short_reason"] = True

        # (4) override=True with proper reason → 200 verifier=admin_override
        r = _call(ecn_id, {"environment": "DEMO", "password": ADMIN_PASSWORD, "override": True,
                           "reason": "practice account confirmed with broker rep"})
        if r.status_code != 429:
            assert r.status_code == 200, f"override→expected 200 got {r.status_code} {r.text[:300]}"
            row = r.json()
            assert row["effective"] == "DEMO" and row["verifier"] == "admin_override"
            audit = db.admin_audit_log.find_one({"target_id": str(ecn_id),
                                                 "action": "account_environment_attest"},
                                                sort=[("_id", -1)])
            assert audit is not None, "no audit entry for override attest"
            assert audit["meta"]["override"] is True
            assert audit["meta"]["verifier"] == "admin_override"
            assert audit["meta"].get("proof_id")
            checked["override_good"] = True

        # (5) fully proven demo → 200 verifier=ea_heartbeat
        r = _call(good_id, {"environment": "DEMO", "password": ADMIN_PASSWORD, "reason": "demo proven"})
        if r.status_code != 429:
            assert r.status_code == 200, f"good→expected 200 got {r.status_code} {r.text[:300]}"
            row = r.json()
            assert row["effective"] == "DEMO" and row["verifier"] == "ea_heartbeat"
            assert row["attestation_state"] == "valid"
            checked["proven_demo"] = True

        # At least 3 of 5 sub-cases must have gotten past the reauth rate-limit;
        # otherwise the suite was re-run inside the 300 s window — skip rather
        # than false-fail.
        hit = sum(1 for v in checked.values() if v)
        if hit < 3:
            pytest.skip(f"admin_reauth 5/300s rate-limited after {hit}/5 sub-cases: {checked}")
        print(f"attest sub-cases verified: {checked}")
    finally:
        db.accounts.delete_many({"_id": {"$in": [stale_id, ecn_id, good_id]}})
        db.admin_audit_log.delete_many({"target_id": {"$in": [str(stale_id), str(ecn_id), str(good_id)]}})


def test_account_environments_rows_expose_proof_and_verifier(admin_session, db):
    """GET /api/admin/account-environments rows must include proof{} + verifier + attestation_state."""
    uid = str(ObjectId())
    good_doc = _acc(uid, "list")
    good_id = db.accounts.insert_one(good_doc).inserted_id
    try:
        r = admin_session.get(f"{BASE_URL}/api/admin/account-environments", timeout=20)
        assert r.status_code == 200, r.text[:300]
        data = r.json()
        assert "accounts" in data
        row = next((x for x in data["accounts"] if x["account_id"] == str(good_id)), None)
        assert row is not None, "seeded demo account missing from listing"
        assert "proof" in row and isinstance(row["proof"], dict)
        for k in ("ok", "mandatory_ok", "override_eligible", "checks", "reported_server"):
            assert k in row["proof"], f"proof.{k} missing in row {row}"
        assert row["proof"]["mandatory_ok"] is True
        assert row["proof"]["ok"] is True
        assert row["proof"]["reported_server"] == "VTMarkets-Demo"
        assert "verifier" in row  # may be None pre-attest
        assert "attestation_state" in row
    finally:
        db.accounts.delete_many({"_id": good_id})


# ─── P2-01 verified-performance: share refused while stale ───
def test_verified_performance_share_refused_when_stale(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/performance/verified", timeout=20)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    prov = data.get("provenance") or {}
    assert "as_of" in prov and "stale" in prov and "share_allowed" in prov
    # In preview, admin has no reconciled accounts → provenance is stale
    if not prov["stale"]:
        pytest.skip("provenance not stale in preview — skipping stale-share refusal check")
    r2 = admin_session.post(f"{BASE_URL}/api/performance/share", timeout=20)
    assert r2.status_code >= 400 and r2.status_code < 500, \
        f"expected 4xx when stale, got {r2.status_code} {r2.text[:300]}"
    body = r2.json().get("detail") if r2.headers.get("content-type", "").startswith("application/json") else {}
    # One of the attestation/stale reasons must appear
    txt = r2.text
    assert "attestation_gate_failed" in txt or "PROVENANCE_STALE" in txt or "UNVERIFIED" in txt or "stale" in txt.lower(), txt[:300]


# ─── P2-04 release-readiness exposes secrets_rewrap check ───
def test_release_readiness_secrets_rewrap():
    if not METRICS_TOKEN:
        pytest.skip("METRICS_TOKEN not configured")
    r = requests.get(f"{BASE_URL}/api/ops/release-readiness",
                     headers={"Authorization": f"Bearer {METRICS_TOKEN}"}, timeout=20)
    assert r.status_code in (200, 503), r.text[:300]
    checks = r.json().get("checks") or r.json()
    assert "secrets_rewrap" in checks, f"keys: {list(checks.keys())[:20]}"
    rr = checks["secrets_rewrap"]
    assert "ok" in rr and "detail" in rr


# ─── P2-04 vault rewrap endpoint ───
def test_admin_vault_rewrap(admin_session, db):
    r = admin_session.post(f"{BASE_URL}/api/admin/integrations/rewrap",
                           json={"password": ADMIN_PASSWORD, "reason": "r29 http rewrap test"},
                           timeout=30)
    if r.status_code == 429:
        pytest.skip(f"admin_reauth rate-limited: {r.text[:200]}")
    assert r.status_code == 200, r.text[:400]
    out = r.json()
    assert out.get("status") == "complete"
    assert "total" in out and "rewrapped" in out and "to_key_version" in out
    assert isinstance(out["total"], int)
    assert isinstance(out["rewrapped"], (list, dict))
    manifest_id = out.get("manifest_id")
    if manifest_id:
        mdoc = db.secrets_rewrap_manifests.find_one({"_id": manifest_id})
        assert mdoc is not None, f"manifest {manifest_id} not found"
    audit = db.admin_audit_log.find_one({"actor_email": ADMIN_EMAIL, "action": "vault_rewrap"},
                                        sort=[("_id", -1)])
    assert audit is not None, "no admin_audit_log entry for vault_rewrap"


# ─── Admin cannot checkout (grandfathered) — verifies the guard; the full
#     snapshot/pricing_version/idempotency_key assertion lives in the
#     integration test (test_r29_audit.py) which uses a non-admin user ───
def test_checkout_blocks_admin():
    s = requests.Session()
    if not ADMIN_PASSWORD:
        pytest.skip("TEST_ADMIN_PASSWORD not in env")
    r = s.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=20)
    if r.status_code != 200:
        pytest.skip("admin login failed")
    allowed_origins = os.environ.get("CHECKOUT_ALLOWED_ORIGINS", "").split(",")
    origin = (allowed_origins[0].strip() if allowed_origins and allowed_origins[0].strip()
              else BASE_URL)
    r = s.post(f"{BASE_URL}/api/subscription/checkout",
               json={"plan_id": "trader_monthly", "origin": origin}, timeout=20)
    assert r.status_code == 400, r.text[:300]
    assert "grandfather" in r.text.lower() or "admin" in r.text.lower()
