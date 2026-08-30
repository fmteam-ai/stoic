"""HTTP-level verification for audit v3 corrections (iter-166).

Covers over the wire (cookie auth, admin@trading.bot / 6-account admin):
- GET /api/state/inventory  (canonical inventory shape + counters)
- GET /api/bot/health       (context surfaces canonical counters)
- GET /api/bot/pulse        (unbound flag on default profile)
"""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "frontend", ".env"))

pytestmark = pytest.mark.http

from live_target import require_live_base_url

BASE_URL = require_live_base_url()

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=30)
    assert r.status_code == 200, (r.status_code, r.text[:400])
    # CSRF echo (double-submit)
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


class TestInventoryEndpoint:
    def test_auth_required(self):
        r = requests.get(f"{BASE_URL}/api/state/inventory", timeout=15)
        assert r.status_code in (401, 403), (r.status_code, r.text[:200])

    def test_inventory_canonical_shape(self, session):
        r = session.get(f"{BASE_URL}/api/state/inventory", timeout=30)
        assert r.status_code == 200, (r.status_code, r.text[:400])
        inv = r.json()
        # top-level counters
        for k in ("accounts_configured", "accounts_enabled",
                  "bots_requested_on", "ea_connection",
                  "ea_installation_count", "accounts"):
            assert k in inv, f"missing {k}: keys={list(inv.keys())}"
        # 6-account admin
        assert inv["accounts_configured"] == 6, inv
        assert inv["accounts_enabled"] == 3, inv
        assert inv["bots_requested_on"] == 3, inv
        ea = inv["ea_connection"]
        for bucket in ("fresh", "stale", "offline", "paper"):
            assert bucket in ea, ea
            assert isinstance(ea[bucket], int), ea
        assert (ea["fresh"] + ea["stale"] + ea["offline"] + ea["paper"]
                == 6), ea
        assert isinstance(inv["ea_installation_count"], int)
        assert len(inv["accounts"]) == 6
        row = inv["accounts"][0]
        for k in ("account_enabled", "bot_requested_enabled",
                  "bot_effective_state", "ea_connection_state",
                  "environment"):
            assert k in row, row


class TestBotHealthContext:
    def test_health_context_carries_inventory(self, session):
        r = session.get(f"{BASE_URL}/api/bot/health-score", timeout=30)
        assert r.status_code == 200, (r.status_code, r.text[:400])
        body = r.json()
        # health may return context directly or nested
        ctx = body.get("context") or body
        # locate canonical counters (accept either accounts_total or
        # accounts_configured naming; audit v3 uses accounts_total inside
        # context per review)
        acc_total = (ctx.get("accounts_total")
                     or ctx.get("accounts_configured"))
        assert acc_total == 6, (
            "context accounts_total/accounts_configured should be 6",
            ctx)
        assert ctx.get("accounts_enabled") == 3, ctx
        assert ctx.get("bots_requested_on") == 3, ctx
        ea = ctx.get("ea_connection")
        assert isinstance(ea, dict), ctx
        for k in ("fresh", "stale", "offline", "paper"):
            assert k in ea, ea
        assert (ea["fresh"] + ea["stale"] + ea["offline"] + ea["paper"]
                == 6), ea


class TestBotPulseUnbound:
    def test_pulse_default_profile_unbound_flag(self, session):
        r = session.get(f"{BASE_URL}/api/bot/pulse", timeout=30)
        assert r.status_code == 200, (r.status_code, r.text[:400])
        body = r.json()
        items = body.get("items") or body.get("bots") or body
        assert isinstance(items, list), body
        # every item should have 'unbound'
        assert all("unbound" in it for it in items), items[:2]
        default_items = [it for it in items
                         if it.get("account_id") in (None, "", "null")]
        bound_items = [it for it in items
                       if it.get("account_id") not in (None, "", "null")]
        # at least one default profile expected, all with unbound=True
        assert default_items, "expected at least one default (unbound) profile"
        assert all(it["unbound"] is True for it in default_items), \
            default_items
        # bound configs must be unbound=False
        for it in bound_items:
            assert it["unbound"] is False, it


class TestConsistencyBetweenSurfaces:
    def test_inventory_matches_account_overview(self, session):
        inv = session.get(f"{BASE_URL}/api/state/inventory",
                          timeout=30).json()
        ov = session.get(f"{BASE_URL}/api/accounts/overview",
                         timeout=30)
        if ov.status_code != 200:
            pytest.skip(f"accounts/overview {ov.status_code}")
        o = ov.json()
        totals = o.get("totals") or o
        # ea_fresh / ea_paper introduced by audit v3
        assert totals.get("ea_fresh") == inv["ea_connection"]["fresh"], (
            totals, inv["ea_connection"])
        assert totals.get("ea_paper") == inv["ea_connection"]["paper"], (
            totals, inv["ea_connection"])
