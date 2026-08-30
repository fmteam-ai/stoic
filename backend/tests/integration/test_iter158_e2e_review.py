"""
E2E API tests for iter-158 P0 Phase A review request.

Covers:
 - GET /api/state/contract auth + shape + stale/null broker semantics + totals
 - GET /api/bot/quick-actions new fields + preserved regression fields
 - POST /api/accounts/{id}/test-trade fail-closed on stale + never-connected accounts
 - POST /api/panic still works for admin
"""
import os
import random
import string
import time
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    pytest.skip("REACT_APP_BACKEND_URL not set — live-stack e2e review "
                "suite runs in preview only", allow_module_level=True)

ADMIN_EMAIL = "admin@stoicaibot.com"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(
        f"{BASE_URL}/api/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "csrf_token cookie missing"
    s.headers.update({"X-CSRF-Token": csrf})
    bypass = os.environ.get("STEP_UP_BYPASS_TOKEN") or "qX9mR2vLp8Kt4Wc7Zn3FbYh6JdS5GaEu"
    s.headers.update({"X-Step-Up-Bypass": bypass})
    return s


# ---------- Auth ----------
def test_state_contract_requires_auth():
    r = requests.get(f"{BASE_URL}/api/state/contract", timeout=15)
    assert r.status_code == 401, f"expected 401 anon, got {r.status_code}"


# ---------- state/contract shape ----------
def test_state_contract_shape_and_stale_semantics(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/state/contract", timeout=20)
    assert r.status_code == 200, r.text[:400]
    data = r.json()
    assert "as_of" in data
    assert "position_truth" in data
    assert "accounts" in data
    assert "totals" in data

    totals = data["totals"]
    for key in [
        "accounts_total",
        "accounts_enabled",
        "bots_enabled",
        "eas_connected",
        "open_local",
        "open_broker",
    ]:
        assert key in totals, f"totals missing {key}"

    # Preview has ≥1 stale account → open_broker MUST be null (never fabricated 0)
    # unless every account is fresh (unlikely in preview).
    if data["position_truth"] in ("STALE", "UNKNOWN"):
        assert totals["open_broker"] is None, (
            f"open_broker must be null when position_truth={data['position_truth']}, "
            f"got {totals['open_broker']}"
        )

    for acct in data["accounts"]:
        for k in [
            "account_enabled",
            "bot_enabled",
            "ea_connected",
            "heartbeat_age_seconds",
            "position_truth",
            "open_positions_broker",
            "open_positions_local",
            "execution_authority",
            "effective_state",
            "force_trade_allowed",
        ]:
            assert k in acct, f"account missing {k}: {acct}"
        # If per-account truth is STALE/UNKNOWN, broker count must be null
        if acct["position_truth"] in ("STALE", "UNKNOWN"):
            assert acct["open_positions_broker"] is None, (
                f"account broker count must be null when truth={acct['position_truth']}"
            )


# ---------- quick-actions ----------
def test_quick_actions_has_new_and_original_fields(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/bot/quick-actions", timeout=20)
    assert r.status_code == 200, r.text[:400]
    d = r.json()
    # New iter-158 fields
    for k in [
        "position_truth",
        "open_trades_broker",
        "effective_state",
        "panic_available",
        "accounts_total",
        "accounts_enabled",
        "bots_enabled",
        "eas_connected",
        "as_of",
    ]:
        assert k in d, f"quick-actions missing new field: {k}. keys={list(d.keys())}"
    # Regression: original fields preserved
    for k in ["bot_active", "open_trades", "todays_pnl_usd"]:
        assert k in d, f"quick-actions missing legacy field: {k}"

    # broker count null when not fresh
    if d["position_truth"] in ("STALE", "UNKNOWN"):
        assert d["open_trades_broker"] is None


# ---------- test-trade fail-closed on stale ----------
def _get_first_account(session):
    r = session.get(f"{BASE_URL}/api/accounts", timeout=15)
    assert r.status_code == 200, r.text[:200]
    payload = r.json()
    accts = payload if isinstance(payload, list) else payload.get("accounts", [])
    assert accts, "no accounts in preview"
    return accts[0]


def test_test_trade_fail_closed_on_stale_preview(admin_session):
    acct = _get_first_account(admin_session)
    aid = acct.get("id") or acct.get("_id") or acct.get("account_id")
    r = admin_session.post(
        f"{BASE_URL}/api/accounts/{aid}/test-trade", json={}, timeout=20
    )
    assert r.status_code == 409, f"expected 409 stale gate, got {r.status_code}: {r.text[:200]}"
    body = r.json()
    code = body.get("code") or body.get("detail", {}).get("code") if isinstance(body.get("detail"), dict) else body.get("code")
    text = str(body).lower()
    assert ("stale_ea" in text) or ("ea_never_connected" in text), f"unexpected body: {body}"


def test_test_trade_fail_closed_on_never_connected(admin_session):
    # Create ephemeral account with no heartbeat
    rand = "".join(random.choices(string.digits, k=9))
    payload = {
        "account_number": f"TEST{rand}",
        "broker": "MetaQuotes-Demo",
        "server": "MetaQuotes-Demo",
        "account_name": f"iter158-nc-{rand}",
        "account_type": "demo",
        "label": f"iter158-nc-{rand}",
    }
    r = admin_session.post(f"{BASE_URL}/api/accounts", json=payload, timeout=20)
    if r.status_code not in (200, 201):
        pytest.skip(f"cannot create account: {r.status_code} {r.text[:200]}")
    created = r.json()
    aid = created.get("id") or created.get("_id") or created.get("account_id")
    assert aid, f"no id in create response: {created}"
    try:
        r2 = admin_session.post(
            f"{BASE_URL}/api/accounts/{aid}/test-trade", json={}, timeout=20
        )
        assert r2.status_code == 409, f"expected 409, got {r2.status_code}: {r2.text[:200]}"
        text = r2.text.lower()
        assert "ea_never_connected" in text or "stale_ea" in text, r2.text[:200]
    finally:
        admin_session.delete(f"{BASE_URL}/api/accounts/{aid}?force=true", timeout=20)


# ---------- panic still works ----------
def test_panic_endpoint_admin(admin_session):
    r = admin_session.post(f"{BASE_URL}/api/panic", json={}, timeout=20)
    assert r.status_code in (200, 202), f"panic returned {r.status_code}: {r.text[:200]}"
