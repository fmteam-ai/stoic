"""HTTP-level verification of r28 audit remediation (P1-01, P2-01..05).

Runs against REACT_APP_BACKEND_URL. Uses admin cookie auth.
"""
import os
import sys
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.http
import requests
from bson import ObjectId

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
    assert "access_token" in s.cookies, "login did not set access_token cookie"
    return s


# ─── P2-04 / P2-01: public plans carry minor amounts + pricing_version ───
def test_subscription_plans_shape():
    r = requests.get(f"{BASE_URL}/api/subscription/plans", timeout=15)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    plans = data["plans"] if isinstance(data, dict) and "plans" in data else data
    assert isinstance(plans, list) and len(plans) > 0
    for p in plans:
        assert "amount_minor" in p and isinstance(p["amount_minor"], int)
        assert p["amount_minor"] == p["amount_cents"], f"amount_minor != amount_cents for {p.get('id')}"
        assert "effective_monthly_minor" in p and isinstance(p["effective_monthly_minor"], int)
        assert "savings_minor" in p and isinstance(p["savings_minor"], int)
        assert "currency" in p and isinstance(p["currency"], str)
        assert "pricing_version" in p and isinstance(p["pricing_version"], int) and p["pricing_version"] >= 1
        # deprecated USD fields still present
        assert "amount_usd" in p and "effective_monthly_usd" in p and "savings_usd" in p


# ─── P1-01: admin account-environments listing ───
def test_admin_account_environments_listing(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/account-environments", timeout=20)
    assert r.status_code == 200, r.text[:400]
    data = r.json()
    assert "accounts" in data
    for row in data["accounts"]:
        for key in ("declared", "effective", "attestation_state", "identity_hash", "account_id"):
            assert key in row, f"missing {key} in {row}"
        assert row["declared"] == "DEMO" or row["attestation_state"] in ("valid", "invalidated")


# ─── P1-01: attestation flow + revoke, bad password → 401, LIVE-declared → 409 ───
def test_admin_attest_flow(admin_session):
    from pymongo import MongoClient
    mclient = MongoClient(os.environ["MONGO_URL"])
    db = mclient[os.environ["DB_NAME"]]
    uid = str(ObjectId())
    demo_doc = {"user_id": uid, "label": "r28-http-demo", "mode": "live", "account_type": "demo",
                "broker": "VT Markets", "server": "VTMarkets-Demo", "broker_server": "VTMarkets-Demo",
                "account_number": f"demo-{uid[-6:]}", "bridge_token": f"r28http-demo-{uid}",
                "ea_version": "1.57", "creds_version": 0,
                "ea_identity": {"installation_id": f"inst-{uid[-6:]}", "authoritative": True}}
    live_doc = {**demo_doc, "account_type": "live", "bridge_token": f"r28http-live-{uid}",
                "account_number": f"live-{uid[-6:]}", "server": "VTMarkets-Live", "broker_server": "VTMarkets-Live"}
    demo_id = db.accounts.insert_one(demo_doc).inserted_id
    live_id = db.accounts.insert_one(live_doc).inserted_id
    try:
        # wrong password → 401 reauth_failed (skip if rate limited by prior runs)
        r = admin_session.post(f"{BASE_URL}/api/admin/account-environments/{demo_id}",
                               json={"environment": "DEMO", "password": "wrong-password-xyz",
                                     "reason": "r28-http test"}, timeout=20)
        if r.status_code == 429:
            pytest.skip(f"admin_reauth rate limited (5/300s): {r.text[:200]}")
        assert r.status_code == 401, f"expected 401 got {r.status_code} {r.text[:200]}"
        body = r.json()
        assert body.get("detail", {}).get("code") == "reauth_failed" or "reauth" in str(body).lower()

        # LIVE-declared account cannot be attested as DEMO → 409 declared_not_demo
        r = admin_session.post(f"{BASE_URL}/api/admin/account-environments/{live_id}",
                               json={"environment": "DEMO", "password": ADMIN_PASSWORD,
                                     "reason": "r28-http test"}, timeout=20)
        assert r.status_code == 409, f"expected 409 got {r.status_code} {r.text[:200]}"
        assert "declared_not_demo" in r.text

        # Valid attest
        r = admin_session.post(f"{BASE_URL}/api/admin/account-environments/{demo_id}",
                               json={"environment": "DEMO", "password": ADMIN_PASSWORD,
                                     "reason": "r28-http test"}, timeout=20)
        assert r.status_code == 200, r.text[:300]
        row = r.json()
        assert row["effective"] == "DEMO" and row["attestation_state"] == "valid"
        assert len(row["identity_hash"]) == 64

        # Credential change invalidates
        db.accounts.update_one({"_id": demo_id}, {"$inc": {"creds_version": 1}})
        r = admin_session.get(f"{BASE_URL}/api/admin/account-environments", timeout=20)
        found = next((x for x in r.json()["accounts"] if x["account_id"] == str(demo_id)), None)
        assert found is not None, "demo acc not in listing after cred change"
        assert found["effective"] == "LIVE" and found["attestation_state"] == "invalidated"

        # Revoke via environment=LIVE
        r = admin_session.post(f"{BASE_URL}/api/admin/account-environments/{demo_id}",
                               json={"environment": "LIVE", "password": ADMIN_PASSWORD,
                                     "reason": "r28-http revoke"}, timeout=20)
        assert r.status_code == 200, r.text[:300]
    finally:
        db.accounts.delete_many({"_id": {"$in": [demo_id, live_id]}})
        db.admin_audit_log.delete_many({"target_id": {"$in": [str(demo_id), str(live_id)]}})
        mclient.close()


# ─── P2-02: integrations status + ops release-readiness ───
def test_integrations_status(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/admin/integrations", timeout=20)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert "master_key_source" in data
    assert data["master_key_source"] in ("derived_from_jwt_secret", "dedicated")
    assert "master_key_id" not in data
    assert "vault_readiness" in data
    vr = data["vault_readiness"]
    for k in ("ok", "undecryptable_keys", "detail"):
        assert k in vr


def test_release_readiness_secrets_vault():
    if not METRICS_TOKEN:
        pytest.skip("METRICS_TOKEN not configured")
    r = requests.get(f"{BASE_URL}/api/ops/release-readiness",
                     headers={"Authorization": f"Bearer {METRICS_TOKEN}"}, timeout=20)
    # endpoint may 503 overall if other checks fail (e.g. workers in preview);
    # we only validate that secrets_vault check is present and ok
    assert r.status_code in (200, 503), r.text[:300]
    data = r.json()
    checks = data.get("checks") or data
    assert "secrets_vault" in checks, f"keys: {list(checks.keys())[:20]}"
    assert checks["secrets_vault"]["ok"] is True


# ─── P2-05 provenance contract ───
def _assert_prov(prov, expected_kind):
    for k in ("contract_version", "provider", "source_kind", "as_of", "timezone",
              "freshness_s", "stale", "points", "missing_intervals",
              "missing_intervals_count", "cache_status", "environment", "note"):
        assert k in prov, f"missing provenance field {k}: {prov}"
    assert prov["contract_version"] == 1
    assert prov["timezone"] == "UTC"
    assert prov["source_kind"] == expected_kind


def test_provenance_market_history(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/market/history/EURUSD", timeout=25)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert "provenance" in data
    _assert_prov(data["provenance"], "indicative")


def test_provenance_equity_curve(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/accounts/equity-curve", timeout=25)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert "provenance" in data
    _assert_prov(data["provenance"], "derived")


def test_provenance_verified_performance(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/performance/verified", timeout=25)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert "provenance" in data
    _assert_prov(data["provenance"], "broker_reconciled")


def test_provenance_analytics_research(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/analytics/research?days=90", timeout=25)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert "provenance" in data
    _assert_prov(data["provenance"], "simulated")


# ─── Account serializer should expose env + attestation_state, not raw doc ───
def test_accounts_serializer_fields(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/accounts", timeout=25)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    accts = data if isinstance(data, list) else data.get("accounts", [])
    for a in accts[:5]:
        assert "environment" in a
        assert "environment_attested" in a and isinstance(a["environment_attested"], bool)
        assert a.get("environment_attestation_state") in ("none", "valid", "invalidated")
        assert "environment_attestation" not in a
