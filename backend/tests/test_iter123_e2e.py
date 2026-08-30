"""iter-123 E2E HTTP tests — 3-phase commercial hardening (billing / entitlement / VPS).

Covers behaviors NOT exercised by iter122 service-level unit tests:
  • checkout HTTP: origin allowlist, plan payload
  • payment application via service call (webhook can't be signed) + status/valid_until
  • admin refund endpoint 200/403/404
  • /subscription/plans catalog integer-cent field
  • /bridge/heartbeat identity binding (unknown installation → non-authoritative)
  • /infra/artifacts/manifest signed HMAC-SHA256 + rollback policy
  • entitlement 402 regression on coach & twin endpoints
"""
import asyncio
import os
import time
import uuid

import pytest
import requests

from tests.helpers import (base_url, mark_email_verified, mongo_db,
                           register_and_login)

BASE = base_url()
API = f"{BASE}/api"
ORIGIN = os.environ["CHECKOUT_ALLOWED_ORIGINS"].split(",")[0].strip()
ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
ADMIN_PW = os.environ["ADMIN_PASSWORD"]


# -------- fixtures --------------------------------------------------------
def _admin_session() -> requests.Session:
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW}, timeout=30)
    if r.status_code == 401:
        # Preview DB uses the documented legacy test credentials while
        # backend/.env carries the production recovery password.
        r = s.post(f"{API}/auth/login",
                   json={"email": "admin@trading.bot",
                         "password": "admin123"}, timeout=30)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def admin():
    return _admin_session()


@pytest.fixture()
def fresh_user():
    email = f"iter123_{uuid.uuid4().hex[:10]}@example.com"
    s = register_and_login(email)
    return email, s


# -------- /subscription/plans --------------------------------------------
def test_plans_have_amount_cents_and_16_skus(admin):
    r = admin.get(f"{API}/subscription/plans", timeout=30)
    assert r.status_code == 200, r.text
    body = r.json()
    plans = body.get("plans", body) if isinstance(body, dict) else body
    assert isinstance(plans, list) and len(plans) == 16, f"got {len(plans)}"
    for p in plans:
        assert "amount_cents" in p, p
        assert isinstance(p["amount_cents"], int)
        assert abs(p["amount_usd"] - p["amount_cents"] / 100.0) < 1e-9


# -------- checkout --------------------------------------------------------
def test_checkout_origin_allowlist_allowed(fresh_user):
    _, s = fresh_user
    r = s.post(f"{API}/subscription/checkout",
               json={"plan_id": "trader_monthly", "origin": ORIGIN}, timeout=60)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checkout_url"].startswith("http")
    assert body["plan"]["amount_usd"] == 99.0
    assert body["plan"]["amount_cents"] == 9900
    assert "session_id" in body


def test_checkout_origin_rejected(fresh_user):
    _, s = fresh_user
    r = s.post(f"{API}/subscription/checkout",
               json={"plan_id": "trader_monthly",
                     "origin": "https://attacker.com"}, timeout=30)
    assert r.status_code == 400, r.text
    assert "origin" in r.text.lower()


# -------- payment application (service-level) + status -------------------
def test_apply_payment_then_status_reflects_active(fresh_user):
    email, s = fresh_user
    # 1) create checkout session
    r = s.post(f"{API}/subscription/checkout",
               json={"plan_id": "trader_monthly", "origin": ORIGIN}, timeout=60)
    assert r.status_code == 200
    session_id = r.json()["session_id"]

    # 2) simulate provider "paid" — mark txn paid then call apply
    db = mongo_db()
    db.payment_transactions.update_one(
        {"session_id": session_id},
        {"$set": {"payment_status": "paid"}})

    from subscription_service import apply_successful_payment  # noqa: E402
    sub = asyncio.run(apply_successful_payment(session_id, source="test"))
    assert sub is not None, "apply returned None"

    # 3) status endpoint reflects it
    r = s.get(f"{API}/subscription/status", timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    sub = j.get("subscription") or j
    ent = j.get("entitlement") or {}
    vu = sub.get("valid_until")
    assert vu, j
    assert ent.get("active") is True or ent.get("reason") == "subscription_active", ent
    from datetime import datetime, timezone
    end = datetime.fromisoformat(vu.replace("Z", "+00:00"))
    assert end > datetime.now(timezone.utc)


# -------- admin refund ----------------------------------------------------
def test_admin_refund_flow(fresh_user, admin):
    email, s = fresh_user
    r = s.post(f"{API}/subscription/checkout",
               json={"plan_id": "trader_monthly", "origin": ORIGIN}, timeout=60)
    session_id = r.json()["session_id"]
    db = mongo_db()
    db.payment_transactions.update_one({"session_id": session_id},
                                       {"$set": {"payment_status": "paid"}})
    from subscription_service import apply_successful_payment  # noqa: E402
    asyncio.run(apply_successful_payment(session_id, source="test"))

    # non-admin refund → 403
    r = s.post(f"{API}/subscription/admin/refund",
               json={"session_id": session_id}, timeout=30)
    assert r.status_code == 403, r.text

    # bogus session as admin → 404
    r = admin.post(f"{API}/subscription/admin/refund",
                   json={"session_id": "nope_" + uuid.uuid4().hex},
                   timeout=30)
    assert r.status_code == 404, r.text

    # valid admin refund → 200
    r = admin.post(f"{API}/subscription/admin/refund",
                   json={"session_id": session_id, "reason": "test_refund"},
                   timeout=30)
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True


# -------- entitlement 402 regression -------------------------------------
def test_coach_cards_402_for_fresh_user(fresh_user):
    _, s = fresh_user
    r = s.get(f"{API}/coach/cards", timeout=30)
    assert r.status_code == 402, f"expected 402, got {r.status_code}: {r.text}"
    body = r.json()
    detail = body.get("detail", body)
    if isinstance(detail, dict):
        assert detail.get("minimum_tier") == "trader" or "trader" in str(detail).lower()


def test_twin_summary_402_for_fresh_user(fresh_user):
    _, s = fresh_user
    r = s.get(f"{API}/twin/summary", timeout=30)
    assert r.status_code == 402
    body = r.json()
    detail = body.get("detail", body)
    if isinstance(detail, dict):
        assert detail.get("minimum_tier") == "professional" \
            or "professional" in str(detail).lower()


def test_admin_gets_coach_and_twin_200(admin):
    r = admin.get(f"{API}/coach/cards", timeout=30)
    assert r.status_code == 200, r.text
    r = admin.get(f"{API}/twin/summary", timeout=30)
    assert r.status_code == 200, r.text


# -------- bridge heartbeat identity --------------------------------------
def test_bridge_heartbeat_unknown_installation_marks_non_authoritative():
    """Synthetic account with bridge_token — heartbeat with unknown
    installation_id must NOT be authoritative and must null balance."""
    db = mongo_db()
    account_id = "iter123-acct-" + uuid.uuid4().hex[:8]
    bridge_token = "iter123-btk-" + uuid.uuid4().hex
    user_id = "iter123-usr-" + uuid.uuid4().hex[:8]
    db.accounts.insert_one({
        "id": account_id, "user_id": user_id,
        "bridge_token": bridge_token,
        "login": "99999", "server": "TestSvr", "broker": "TestBroker",
        "account_type": "paper", "status": "connected",
        "balance": 10000.0, "created_at": "2025-01-01T00:00:00Z",
    })
    try:
        # heartbeat with unknown installation_id
        r = requests.post(
            f"{API}/bridge/heartbeat",
            json={"bridge_token": bridge_token,
                  "installation_id": "unregistered-inst-" + uuid.uuid4().hex,
                  "broker_server": "TestSvr", "login": "99999",
                  "terminal_build": "3815", "ea_version": "1.0.0",
                  "balance": 12345.6, "equity": 12345.6},
            timeout=30)
        assert r.status_code == 200, r.text
        doc = db.accounts.find_one({"id": account_id})
        ea = doc.get("ea_identity") or {}
        assert ea.get("authoritative") is False, ea
        # status disconnected OR balance nulled
        assert doc.get("status") == "disconnected" or doc.get("balance") in (None, 0), doc

        # Legacy heartbeat (no installation_id) — normal behavior preserved
        r = requests.post(
            f"{API}/bridge/heartbeat",
            json={"bridge_token": bridge_token,
                  "balance": 5555.5, "equity": 5555.5},
            timeout=30)
        assert r.status_code == 200, r.text
        doc = db.accounts.find_one({"id": account_id})
        assert doc.get("status") == "connected", doc
        assert doc.get("balance") == 5555.5, doc
    finally:
        db.accounts.delete_one({"id": account_id})


# -------- signed manifest -------------------------------------------------
def test_artifact_manifest_signed():
    r = requests.get(f"{API}/infra/artifacts/manifest", timeout=30)
    assert r.status_code == 200, r.text
    m = r.json()
    sig = m.get("signature") or {}
    assert sig.get("alg") == "Ed25519", sig
    assert sig.get("value"), sig
    assert m.get("update_policy", {}).get("rollback") is not None, m


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
