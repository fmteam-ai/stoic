"""SEC-001 BOLA regression: /api/brain/portfolio + /api/brain/regime must
NOT leak another user's account data. Verified against iter-127 fix in
routes/brain_routes.py::_owned_account."""
import time
import uuid

import pytest
import requests

from tests.helpers import base_url, register_and_login


API = f"{base_url()}/api"


def _admin_session() -> requests.Session:
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@stoicaibot.com", "password": "admin123"},
               timeout=30)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def admin():
    return _admin_session()


@pytest.fixture(scope="module")
def admin_account_id(admin):
    r = admin.get(f"{API}/accounts", timeout=30)
    assert r.status_code == 200, r.text
    accs = r.json()
    if isinstance(accs, dict):
        accs = accs.get("accounts") or accs.get("items") or []
    assert isinstance(accs, list) and accs, f"no accounts for admin: {accs}"
    aid = accs[0].get("id") or accs[0].get("_id")
    assert aid
    return str(aid)


@pytest.fixture(scope="module")
def attacker():
    email = f"sec001-attacker-{uuid.uuid4().hex[:8]}@qa.example.com"
    return register_and_login(email)


# -- SEC-001: NON-ADMIN reading admin's account must 404 --------------------
def test_portfolio_bola_blocked(attacker, admin_account_id):
    r = attacker.get(f"{API}/brain/portfolio",
                     params={"account_id": admin_account_id}, timeout=30)
    assert r.status_code == 404, \
        f"BOLA leak: got {r.status_code} body={r.text[:400]}"
    # Ensure no positions/currency_exposure leaked in the error body.
    body = r.text.lower()
    assert "open_positions" not in body and "currency_exposure" not in body


def test_regime_bola_blocked(attacker, admin_account_id):
    r = attacker.get(f"{API}/brain/regime",
                     params={"symbol": "XAUUSD",
                             "account_id": admin_account_id}, timeout=30)
    assert r.status_code == 404, \
        f"BOLA leak: got {r.status_code} body={r.text[:400]}"


# -- Positive: admin can inspect any account -------------------------------
def test_portfolio_admin_ok(admin, admin_account_id):
    r = admin.get(f"{API}/brain/portfolio",
                  params={"account_id": admin_account_id}, timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    for k in ("open_positions", "currency_exposure", "stress", "limits"):
        assert k in j, f"missing key {k} in {j.keys()}"


# -- Edge: nonexistent + malformed account_id ------------------------------
def test_portfolio_nonexistent_returns_404(admin):
    r = admin.get(f"{API}/brain/portfolio",
                  params={"account_id": "000000000000000000000000"},
                  timeout=30)
    assert r.status_code == 404, r.text


def test_portfolio_malformed_account_id(admin):
    r = admin.get(f"{API}/brain/portfolio",
                  params={"account_id": "abc"}, timeout=30)
    # Must NOT be 500 — accept 400/404/422.
    assert r.status_code in (400, 404, 422), \
        f"expected 4xx, got {r.status_code}: {r.text[:300]}"


# -- Regression: symbol-only routes still work for any authed user --------
def test_regime_symbol_only_ok(attacker):
    r = attacker.get(f"{API}/brain/regime",
                     params={"symbol": "XAUUSD"}, timeout=30)
    assert r.status_code == 200, r.text


def test_router_symbol_only_ok(attacker):
    r = attacker.get(f"{API}/brain/router",
                     params={"symbol": "XAUUSD"}, timeout=30)
    assert r.status_code == 200, r.text
    j = r.json()
    assert "market_state" in j
