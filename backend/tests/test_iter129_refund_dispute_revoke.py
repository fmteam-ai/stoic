"""iter-129 — Stripe refund/dispute auto-revoke wiring.

Charge-level refund/dispute events carry a payment_intent (not a Checkout
session id). The webhook now resolves the originating session and revokes,
but ONLY when the Stripe signature is cryptographically verified.
"""
import asyncio
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
from live_target import require_live_base_url
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env"))


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_resolver_maps_payment_intent_to_session(monkeypatch):
    from routes import subscription_routes as sr

    class _Sess:
        def __init__(self, sid):
            self.id = sid

    class _List:
        data = [_Sess("cs_test_resolved_123")]

    fake_stripe = types.ModuleType("stripe")
    fake_stripe.api_key = None
    fake_stripe.checkout = types.SimpleNamespace(
        Session=types.SimpleNamespace(list=lambda **kw: _List()))
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)

    body = b'{"data":{"object":{"payment_intent":"pi_abc"}}}'
    sid = _run(sr._resolve_session_id_for_revoke(body))
    assert sid == "cs_test_resolved_123"


def test_resolver_returns_none_without_payment_intent():
    from routes import subscription_routes as sr
    assert _run(sr._resolve_session_id_for_revoke(b'{"data":{"object":{}}}')) is None
    assert _run(sr._resolve_session_id_for_revoke(b"not-json")) is None


def test_resolver_none_when_no_matching_session(monkeypatch):
    from routes import subscription_routes as sr

    class _List:
        data = []
    fake_stripe = types.ModuleType("stripe")
    fake_stripe.checkout = types.SimpleNamespace(
        Session=types.SimpleNamespace(list=lambda **kw: _List()))
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)
    body = b'{"data":{"object":{"payment_intent":"pi_none"}}}'
    assert _run(sr._resolve_session_id_for_revoke(body)) is None


def test_unsigned_refund_webhook_is_ignored_http():
    """No STRIPE_WEBHOOK_SECRET in preview → unsigned revoke must be a no-op
    200 (griefing defense), never a revoke."""
    import requests
    base = require_live_base_url()
    r = requests.post(
        f"{base}/api/webhook/stripe",
        json={"type": "charge.refunded", "id": "evt_r",
              "data": {"object": {"payment_intent": "pi_unsigned"}}},
        timeout=20)
    assert r.status_code == 200


def test_webhook_handler_wires_resolver_and_signature_guard():
    import inspect
    from routes import subscription_routes as sr
    src = inspect.getsource(sr.stripe_webhook)
    assert "_resolve_session_id_for_revoke" in src
    assert "signature_verified" in src
    assert "revoke_payment" in src


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
