"""iter-120 4-tier subscription HTTP contract suite.

Covers the E1 review request:
  • GET /api/subscription/plans → 16 SKUs, correct bases + discount math.
  • GET /api/entitlements/tiers  → 4 tier matrices with max_operational_mode.
  • Legacy plan aliases via /subscription/checkout (fresh user).
  • Feature gates (402) on twin/research/marketplace/coach/genetics/
    broker-intel/quant-allocator/agents/report-card/api-keys/infra endpoints.
  • Mode ceiling: starter -> supervised_live gated 402; after seeding
    trader_monthly the same PUT gets past the 402 (409 shadow-health
    accepted); professional -> autonomous_live still 402 elite_ai.
  • Account quota: starter first create OK, second create → 402.
  • Admin bypasses ALL gates.
"""
from __future__ import annotations
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import pytest
import requests

from tests.helpers import (
    base_url, mongo_db, register_and_login, mark_email_verified,
)

API = base_url() + "/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


# ============================================================================
# Fixtures
# ============================================================================
def _fresh_email(tag: str = "iter120") -> str:
    return f"TEST_{tag}_{secrets.token_hex(4)}@example.com"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    assert r.status_code == 200, f"admin login failed: {r.text}"
    return s


def _force_starter(email: str) -> None:
    """Clear grace_until so subscription_service resolves the user as
    genuine `starter`. The service unconditionally seeds a 30-day grace
    on ANY non-admin user's subscription doc (see subscription_service.
    get_subscription line ~82), so a truly-fresh user is trader-by-grace
    unless we explicitly wipe grace_until. This is required by the
    review's spec: "FRESH user (starter tier, no subscription)"."""
    db = mongo_db()
    u = db.users.find_one({"email": email.lower()})
    if not u:
        return
    db.subscriptions.update_one(
        {"user_id": str(u["_id"])},
        {"$set": {"grace_until": None, "current_plan_id": None,
                  "valid_until": None}},
        upsert=True,
    )


@pytest.fixture()
def fresh_user_session():
    """A fresh registered+verified user forced to starter tier."""
    email = _fresh_email()
    s = register_and_login(email)
    _force_starter(email)  # zero grace so gates fire correctly
    s._qa_email = email  # type: ignore[attr-defined]
    yield s
    # Cleanup
    try:
        db = mongo_db()
        u = db.users.find_one({"email": email.lower()})
        if u:
            uid = str(u["_id"])
            db.accounts.delete_many({"user_id": uid})
            db.subscriptions.delete_many({"user_id": uid})
            db.payment_transactions.delete_many({"user_id": uid})
        db.users.delete_one({"email": email.lower()})
    except Exception:
        pass


# ============================================================================
# Catalog + tier matrix
# ============================================================================
class TestCatalogHTTP:
    def test_plans_returns_sixteen_skus(self):
        r = requests.get(f"{API}/subscription/plans", timeout=15)
        assert r.status_code == 200, r.text
        plans = r.json()
        assert isinstance(plans, list)
        assert len(plans) == 16, f"expected 16 SKUs, got {len(plans)}"
        ids = {p["id"] for p in plans}
        expected = {
            f"{t}_{d}" for t in ("starter", "trader", "professional", "elite_ai")
            for d in ("monthly", "quarterly", "semi_annual", "annual")
        }
        assert ids == expected

    def test_plans_monthly_bases_are_correct(self):
        r = requests.get(f"{API}/subscription/plans", timeout=15)
        by_id = {p["id"]: p for p in r.json()}
        assert by_id["starter_monthly"]["amount_usd"] == 39.0
        assert by_id["trader_monthly"]["amount_usd"] == 99.0
        assert by_id["professional_monthly"]["amount_usd"] == 199.0
        assert by_id["elite_ai_monthly"]["amount_usd"] == 399.0

    def test_plans_discount_math(self):
        """monthly=0%, quarterly=10%, semi=20%, annual=40%."""
        r = requests.get(f"{API}/subscription/plans", timeout=15)
        by_id = {p["id"]: p for p in r.json()}
        # trader $99 base
        assert by_id["trader_monthly"]["discount_pct"] == 0
        assert by_id["trader_quarterly"]["discount_pct"] == 10
        assert by_id["trader_quarterly"]["amount_usd"] == round(99 * 3 * 0.90, 2)
        assert by_id["trader_semi_annual"]["amount_usd"] == round(99 * 6 * 0.80, 2)
        assert by_id["trader_annual"]["amount_usd"] == round(99 * 12 * 0.60, 2)
        # elite_ai annual 40% off
        assert by_id["elite_ai_annual"]["amount_usd"] == round(399 * 12 * 0.60, 2)

    def test_entitlements_tiers_returns_four_matrices(self):
        r = requests.get(f"{API}/entitlements/tiers", timeout=15)
        assert r.status_code == 200
        m = r.json()
        assert set(m.keys()) == {"starter", "trader", "professional", "elite_ai"}
        # max_operational_mode ceilings match spec
        assert m["starter"]["max_operational_mode"] == "demo_autopilot"
        assert m["trader"]["max_operational_mode"] == "supervised_live"
        assert m["professional"]["max_operational_mode"] == "supervised_live"
        assert m["elite_ai"]["max_operational_mode"] == "autonomous_live"
        # cap fields
        assert m["starter"]["max_accounts"] == 1
        assert m["trader"]["max_accounts"] == 3
        assert m["professional"]["max_accounts"] == 10
        assert m["elite_ai"]["max_accounts"] == 50


# ============================================================================
# Legacy plan aliases via checkout
# ============================================================================
class TestLegacyAliasesCheckout:
    def test_monthly_alias_resolves_to_trader_monthly(self, fresh_user_session):
        """POST /api/subscription/checkout with plan_id 'monthly' must resolve
        to trader_monthly ($99). Fresh non-admin user (admin returns 400)."""
        r = fresh_user_session.post(
            f"{API}/subscription/checkout",
            json={"plan_id": "monthly", "origin": "https://qa.test"},
            timeout=30)
        # Success → plan echoed back must be trader_monthly.
        # Some environments may fail with a 502 (Stripe cannot reach — but
        # the alias resolution happens BEFORE the Stripe call, so if we get
        # a 502 we still know resolution passed the get_plan() check).
        assert r.status_code in (200, 502), r.text
        if r.status_code == 200:
            body = r.json()
            assert body["plan"]["id"] == "trader_monthly"
            assert body["plan"]["amount_usd"] == 99.0

    def test_elite_monthly_alias_resolves_to_professional_monthly(self, fresh_user_session):
        r = fresh_user_session.post(
            f"{API}/subscription/checkout",
            json={"plan_id": "elite_monthly", "origin": "https://qa.test"},
            timeout=30)
        assert r.status_code in (200, 502), r.text
        if r.status_code == 200:
            body = r.json()
            assert body["plan"]["id"] == "professional_monthly"
            assert body["plan"]["amount_usd"] == 199.0

    def test_admin_cannot_checkout(self, admin_session):
        r = admin_session.post(
            f"{API}/subscription/checkout",
            json={"plan_id": "trader_monthly", "origin": "https://qa.test"},
            timeout=30)
        assert r.status_code == 400
        assert "admin" in r.text.lower() or "grandfathered" in r.text.lower()


# ============================================================================
# Feature gates (402) for fresh starter user
# ============================================================================
# Each entry: (method, path, expected minimum_tier). For POSTs we send an
# empty-ish body — the gate fires before payload validation.
FEATURE_GATE_CASES = [
    ("GET",  "/twin/summary",                "professional"),
    ("GET",  "/research/proposals",          "professional"),
    ("GET",  "/marketplace/strategies",      "professional"),
    ("GET",  "/coach/cards",                 "trader"),
    ("GET",  "/genetics/lineage",            "elite_ai"),
    ("GET",  "/broker-intel",                "trader"),
    ("GET",  "/quant/allocator",             "professional"),
    ("GET",  "/agents/report-card",          "professional"),
]


class TestFeatureGates402:
    @pytest.mark.parametrize("method,path,min_tier", FEATURE_GATE_CASES)
    def test_starter_gets_402(self, fresh_user_session, method, path, min_tier):
        url = f"{API}{path}"
        r = fresh_user_session.request(method, url, timeout=20)
        assert r.status_code == 402, (
            f"{method} {path}: expected 402, got {r.status_code} — {r.text[:200]}"
        )
        detail = r.json().get("detail") or r.json()
        # Detail may itself be the dict or nested
        d = detail if isinstance(detail, dict) else {}
        assert d.get("error") == "feature_locked", d
        assert d.get("minimum_tier") == min_tier, d

    def test_api_keys_create_402(self, fresh_user_session):
        r = fresh_user_session.post(f"{API}/api-keys", json={"label": "qa"}, timeout=20)
        assert r.status_code == 402, r.text
        d = r.json().get("detail", {})
        assert d.get("error") == "feature_locked"
        assert d.get("minimum_tier") == "professional"

    def test_infra_pairing_402_trader(self, fresh_user_session):
        # No account required — the router-level gate fires first.
        r = fresh_user_session.post(f"{API}/infra/pairing",
                                    json={"account_id": "dummy"}, timeout=20)
        assert r.status_code == 402, r.text
        d = r.json().get("detail", {})
        assert d.get("error") == "feature_locked"
        assert d.get("minimum_tier") == "trader"

    def test_infra_connect_existing_402_trader(self, fresh_user_session):
        r = fresh_user_session.post(f"{API}/infra/vps/connect-existing",
                                    json={"host": "1.2.3.4"}, timeout=20)
        assert r.status_code == 402, r.text
        d = r.json().get("detail", {})
        assert d.get("minimum_tier") == "trader"

    def test_infra_deployments_402_professional_pathA(self, fresh_user_session):
        # POST /api/infra/deployments — path A (managed provisioning) → professional
        r = fresh_user_session.post(f"{API}/infra/deployments",
                                    json={"provider": "SimulatedProvider"}, timeout=20)
        assert r.status_code == 402, r.text
        d = r.json().get("detail", {})
        assert d.get("minimum_tier") in ("trader", "professional"), d


# ============================================================================
# Admin bypasses all gates
# ============================================================================
class TestAdminBypass:
    @pytest.mark.parametrize("method,path,_min", FEATURE_GATE_CASES)
    def test_admin_not_402(self, admin_session, method, path, _min):
        r = admin_session.request(method, f"{API}{path}", timeout=20)
        # Any non-402 counts as "bypass worked" (200/204/404/409 all fine).
        assert r.status_code != 402, f"admin blocked from {path}: {r.text[:200]}"


# ============================================================================
# Mode ceiling
# ============================================================================
def _seed_subscription(user_email: str, plan_id: str, days: int = 30) -> None:
    db = mongo_db()
    u = db.users.find_one({"email": user_email.lower()})
    assert u, f"no user {user_email}"
    valid = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    db.subscriptions.update_one(
        {"user_id": str(u["_id"])},
        {"$set": {"current_plan_id": plan_id, "valid_until": valid}},
        upsert=True,
    )


class TestModeCeiling:
    def test_starter_supervised_live_402(self, fresh_user_session):
        # Starter -> supervised_live => 402 mode_locked, minimum_tier=trader
        r = fresh_user_session.put(
            f"{API}/bot/config",
            json={"operational_mode": "supervised_live"},
            timeout=20,
        )
        assert r.status_code == 402, r.text
        d = r.json().get("detail", {})
        assert d.get("error") == "mode_locked"
        assert d.get("minimum_tier") == "trader"

    def test_trader_supervised_live_passes_402_gate(self, fresh_user_session):
        # Seed trader_monthly; expect the 402 tier-gate to pass
        # (409 shadow health from cert gate is acceptable).
        _seed_subscription(fresh_user_session._qa_email, "trader_monthly")
        r = fresh_user_session.put(
            f"{API}/bot/config",
            json={"operational_mode": "supervised_live"},
            timeout=20,
        )
        assert r.status_code != 402, (
            f"still 402 after trader upgrade: {r.text[:300]}"
        )
        # 409 with 'shadow' or 'health' in message is expected in preview.
        if r.status_code == 409:
            assert (
                "shadow" in r.text.lower() or "health" in r.text.lower()
                or "certif" in r.text.lower() or "promotion" in r.text.lower()
            ), r.text

    def test_professional_autonomous_live_402(self, fresh_user_session):
        _seed_subscription(fresh_user_session._qa_email, "professional_monthly")
        r = fresh_user_session.put(
            f"{API}/bot/config",
            json={"operational_mode": "autonomous_live"},
            timeout=20,
        )
        assert r.status_code == 402, r.text
        d = r.json().get("detail", {})
        assert d.get("error") == "mode_locked"
        assert d.get("minimum_tier") == "elite_ai"


# ============================================================================
# Account quota
# ============================================================================
class TestAccountQuota:
    def test_starter_second_account_402(self, fresh_user_session):
        payload1 = {
            "label": "TEST_iter120_a1",
            "broker": "demo",
            "server": "demo-server",
            "account_number": "1000001",
            "account_type": "demo",
            "base_currency": "USD",
            "mode": "paper",
        }
        r1 = fresh_user_session.post(f"{API}/accounts", json=payload1, timeout=25)
        assert r1.status_code in (200, 201), f"first account create failed: {r1.status_code} {r1.text[:200]}"
        # Second account create → should now hit the quota gate
        payload2 = dict(payload1, label="TEST_iter120_a2", account_number="1000002")
        r2 = fresh_user_session.post(f"{API}/accounts", json=payload2, timeout=25)
        assert r2.status_code == 402, (
            f"expected 402 quota, got {r2.status_code}: {r2.text[:200]}"
        )
        d = r2.json().get("detail", {})
        assert d.get("error") == "account_quota_exceeded"
        assert d.get("minimum_tier") == "trader"
