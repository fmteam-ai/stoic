"""HTTP live tests for iter-137 Admin → Integrations plans editor + email templates.
Runs against REACT_APP_BACKEND_URL with admin cookie session. Restores defaults at end.
"""
import os
import pytest
import requests

BASE = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
ADMIN_EMAIL = os.environ.get("TEST_ADMIN_EMAIL") or "admin@trading.bot"
ADMIN_PASSWORD = os.environ.get("TEST_ADMIN_PASSWORD") or "R9xcJ7qdJWaYLjlmkjDiC7Vlol3iz#q"

DEFAULTS = {
    "base_cents": {"starter": 3900, "trader": 9900, "professional": 19900, "elite_ai": 39900},
    "discounts": {"quarterly": 10, "semi_annual": 20, "annual": 40},
    "currency": "usd",
    "trial_days": 15,
    "trial_tier": "trader",
}


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
    yield s
    # Restore defaults (best-effort)
    try:
        s.post(f"{BASE}/api/admin/integrations/plans",
               json={**DEFAULTS, "password": ADMIN_PASSWORD}, timeout=15)
    except Exception:
        pass


def test_get_plans_shape(admin_session):
    r = admin_session.get(f"{BASE}/api/admin/integrations/plans", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert set(j["base_cents"].keys()) == {"starter", "trader", "professional", "elite_ai"}
    assert set(j["discounts"].keys()) >= {"quarterly", "semi_annual", "annual"}
    assert j["currency"] in ("usd", "eur", "gbp", "chf", "aud", "cad")
    assert "currency_symbol" in j and "supported_currencies" in j
    assert "trial_days" in j and "trial_tier" in j and "trial_enabled_at" in j
    assert "defaults" in j and "matrix" in j
    assert len(j["matrix"]) == 16
    for row in j["matrix"]:
        assert "currency" in row and "currency_symbol" in row


def test_post_plans_wrong_password_returns_401(admin_session):
    r = admin_session.post(f"{BASE}/api/admin/integrations/plans",
                           json={**DEFAULTS, "password": "wrong-pw"}, timeout=15)
    assert r.status_code == 401
    body = r.json()
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
    assert (detail.get("code") == "reauth_failed") or ("reauth_failed" in str(body))


def test_post_plans_validation_errors(admin_session):
    # non-monotonic
    r = admin_session.post(f"{BASE}/api/admin/integrations/plans",
                           json={**DEFAULTS, "base_cents": {"starter": 10000, "trader": 9900,
                                                             "professional": 19900, "elite_ai": 39900},
                                 "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 422
    # discount too high
    r = admin_session.post(f"{BASE}/api/admin/integrations/plans",
                           json={**DEFAULTS, "discounts": {"quarterly": 10, "semi_annual": 20, "annual": 95},
                                 "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 422
    # bad currency
    r = admin_session.post(f"{BASE}/api/admin/integrations/plans",
                           json={**DEFAULTS, "currency": "xyz", "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 422
    # trial_days too big
    r = admin_session.post(f"{BASE}/api/admin/integrations/plans",
                           json={**DEFAULTS, "trial_days": 500, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code == 422


def test_post_plans_success_applies_and_audits_then_restore(admin_session):
    payload = {
        "base_cents": {"starter": 4900, "trader": 9900, "professional": 19900, "elite_ai": 39900},
        "discounts": {"quarterly": 10, "semi_annual": 20, "annual": 45},
        "currency": "eur",
        "trial_days": 15,
        "trial_tier": "trader",
        "password": ADMIN_PASSWORD,
    }
    r = admin_session.post(f"{BASE}/api/admin/integrations/plans", json=payload, timeout=15)
    assert r.status_code == 200, r.text[:300]

    # public plans reflect override
    r2 = admin_session.get(f"{BASE}/api/subscription/plans", timeout=15)
    assert r2.status_code == 200
    plans = r2.json()
    # response could be list or dict; normalize
    items = plans if isinstance(plans, list) else plans.get("plans") or list(plans.values())
    by_id = {p.get("id") or p.get("plan_id"): p for p in items if isinstance(p, dict)}
    starter_m = by_id.get("starter_monthly")
    starter_a = by_id.get("starter_annual")
    assert starter_m, f"no starter_monthly in {list(by_id.keys())[:10]}"
    assert starter_m["amount_cents"] == 4900
    assert starter_m["currency"] == "eur"
    assert starter_m["currency_symbol"] == "€"
    assert starter_a["amount_cents"] == round(4900 * 12 * 0.55)  # 32340

    # audit log has plan_pricing_update
    ra = admin_session.get(f"{BASE}/api/admin/audit?limit=10", timeout=15)
    if ra.status_code == 200:
        j = ra.json()
        audits = j.get("audit") or j.get("items") or []
        assert any(a.get("action") == "plan_pricing_update" for a in audits), \
            f"no plan_pricing_update in audit: {[a.get('action') for a in audits]}"

    # RESTORE DEFAULTS
    restore = {**DEFAULTS, "password": ADMIN_PASSWORD}
    rr = admin_session.post(f"{BASE}/api/admin/integrations/plans", json=restore, timeout=15)
    assert rr.status_code == 200
    rc = admin_session.get(f"{BASE}/api/admin/integrations/plans", timeout=15).json()
    assert rc["base_cents"]["starter"] == 3900
    assert rc["currency"] == "usd"


def test_non_admin_forbidden():
    s = requests.Session()
    # unauthenticated
    r = s.get(f"{BASE}/api/admin/integrations/plans", timeout=15)
    assert r.status_code in (401, 403)
    r = s.post(f"{BASE}/api/admin/integrations/plans",
               json={**DEFAULTS, "password": ADMIN_PASSWORD}, timeout=15)
    assert r.status_code in (401, 403)


# ---------------- email templates ----------------

def test_email_templates_list_and_render(admin_session):
    r = admin_session.get(f"{BASE}/api/admin/integrations/email-templates", timeout=15)
    assert r.status_code == 200
    j = r.json()
    assert "templates" in j and len(j["templates"]) == 10
    assert "configured" in j and "sender" in j

    r = admin_session.get(f"{BASE}/api/admin/integrations/email-templates/login_otp", timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert "sign-in code" in (body.get("subject") or "").lower()
    assert "123456" in (body.get("html") or "")

    r = admin_session.get(f"{BASE}/api/admin/integrations/email-templates/nope", timeout=15)
    assert r.status_code == 404


def test_email_template_send_validation_and_send(admin_session):
    # invalid recipient
    r = admin_session.post(f"{BASE}/api/admin/integrations/email-templates/login_otp/send",
                           json={"recipient": "not-an-email"}, timeout=20)
    assert r.status_code == 422

    # valid recipient — accept 200 or 503 email_send_failed
    r = admin_session.post(f"{BASE}/api/admin/integrations/email-templates/login_otp/send",
                           json={"recipient": ADMIN_EMAIL}, timeout=25)
    assert r.status_code in (200, 503), r.text[:300]
    if r.status_code == 200:
        j = r.json()
        assert j.get("ok") is True
    else:
        body = r.json()
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        assert detail.get("code") == "email_send_failed"


import pytest as _pytest  # noqa
pytestmark = _pytest.mark.http
