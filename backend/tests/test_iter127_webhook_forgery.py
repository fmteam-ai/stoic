"""iter-127 — Stripe webhook forgery prevention (SEC-001 fix).

A forged/unsigned webhook that claims payment_status=paid for a session the
attacker merely STARTED must NOT grant entitlement or affiliate commission —
the server independently re-verifies with Stripe before applying value.
"""
import os
import sys
import uuid

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE}/api"


def test_forged_paid_webhook_does_not_grant_entitlement():
    """Body-only forged 'paid' event for a fabricated session must be
    rejected (Stripe re-verify fails → 400/402/502), never a 200 that
    would apply payment."""
    fake_session = f"cs_test_forged_{uuid.uuid4().hex}"
    r = requests.post(
        f"{API}/webhook/stripe",
        json={"type": "checkout.session.completed", "id": "evt_forged",
              "data": {"object": {"id": fake_session,
                                  "payment_status": "paid"}}},
        headers={"Content-Type": "application/json"},
        timeout=20,
    )
    # No valid signature AND Stripe cannot confirm the session → must fail.
    assert r.status_code in (400, 402, 502), r.text
    body = r.text.lower()
    assert "ok" not in body or r.status_code >= 400


def test_webhook_invalid_signature_returns_400():
    r = requests.post(
        f"{API}/webhook/stripe",
        data=b"{}",
        headers={"Stripe-Signature": "t=0,v1=bogus",
                 "Content-Type": "application/json"},
        timeout=20,
    )
    assert r.status_code == 400


def test_stripe_client_passes_webhook_secret_when_present():
    import inspect
    from routes import subscription_routes
    src = inspect.getsource(subscription_routes._stripe_client)
    assert "webhook_secret" in src
    assert "STRIPE_WEBHOOK_SECRET" in src


def test_webhook_reverifies_paid_before_apply():
    import inspect
    from routes import subscription_routes
    src = inspect.getsource(subscription_routes.stripe_webhook)
    # Must call get_checkout_status and gate apply on Stripe's answer.
    assert "get_checkout_status" in src
    assert "apply_successful_payment" in src
    # Unsigned revoke events must be guarded by signature verification.
    assert "signature_verified" in src


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
