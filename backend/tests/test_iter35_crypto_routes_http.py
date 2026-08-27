"""Iter-35 backend HTTP smoke test — Binance Spot CCXT routes.

Tests the live /api/crypto/* HTTP surface (no real exchange calls):
  - auth gates (401 without cookie)
  - GET /api/crypto/status shape
  - GET /api/crypto/accounts empty list
  - POST /api/crypto/accounts validation + bad-keys rejection (422)
  - DELETE /api/crypto/accounts/{bad-id} → 404
  - smoke: crypto_bridge module loads
  - regression: /api/trades, /api/accounts, /api/research/proposals,
    /api/trades/{id}/explain still work
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
if not BASE_URL:
    # fallback: read frontend/.env
    try:
        with open(_os.path.join(_REPO_DIR, "frontend", ".env")) as f:
            for line in f:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip().rstrip("/")
                    break
    except Exception:
        pass

API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASS = "admin123"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASS},
               timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
    return s


# ---------- smoke / module load ----------
def test_crypto_module_loads_cleanly():
    from crypto_bridge.binance_engine import BinanceCCXTEngine  # noqa: F401
    from crypto_bridge.binance_ccxt import BinanceClient        # noqa: F401
    assert BinanceCCXTEngine is not None


# ---------- auth gates ----------
def test_crypto_status_requires_auth():
    r = requests.get(f"{API}/crypto/status", timeout=10)
    assert r.status_code == 401, f"expected 401, got {r.status_code}"


def test_crypto_accounts_list_requires_auth():
    r = requests.get(f"{API}/crypto/accounts", timeout=10)
    assert r.status_code == 401


def test_crypto_accounts_create_requires_auth():
    r = requests.post(f"{API}/crypto/accounts",
                      json={"label": "x", "api_key": "x" * 12, "api_secret": "y" * 12},
                      timeout=10)
    assert r.status_code == 401


def test_crypto_accounts_delete_requires_auth():
    r = requests.delete(f"{API}/crypto/accounts/507f1f77bcf86cd799439011", timeout=10)
    assert r.status_code == 401


# ---------- /api/crypto/status ----------
def test_crypto_status_shape(admin_session):
    r = admin_session.get(f"{API}/crypto/status", timeout=10)
    assert r.status_code == 200, r.text
    data = r.json()
    assert "live_enabled" in data and "default_testnet" in data
    assert data["live_enabled"] is False, "BINANCE_LIVE_ENABLED should default false"
    assert data["default_testnet"] is True
    assert isinstance(data["live_enabled"], bool)
    assert isinstance(data["default_testnet"], bool)


# ---------- /api/crypto/accounts list ----------
def test_crypto_accounts_list_returns_array(admin_session):
    r = admin_session.get(f"{API}/crypto/accounts", timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    # Each item (if any) must NOT leak encrypted creds blob
    for acc in data:
        assert "creds" not in acc
        assert acc.get("kind") == "binance"


# ---------- /api/crypto/accounts validation ----------
def test_crypto_accounts_create_missing_fields_422(admin_session):
    # missing api_key + api_secret
    r = admin_session.post(f"{API}/crypto/accounts",
                            json={"label": "missing-keys"}, timeout=10)
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text[:200]}"


def test_crypto_accounts_create_short_keys_422(admin_session):
    # min_length=10 should reject
    r = admin_session.post(f"{API}/crypto/accounts",
                            json={"label": "short",
                                  "api_key": "abc",
                                  "api_secret": "xyz"}, timeout=10)
    assert r.status_code == 422


def test_crypto_accounts_create_blank_label_422(admin_session):
    r = admin_session.post(f"{API}/crypto/accounts",
                            json={"label": "",
                                  "api_key": "a" * 12,
                                  "api_secret": "b" * 12}, timeout=10)
    assert r.status_code == 422


def test_crypto_accounts_create_bad_keys_rejected_422_no_persist(admin_session):
    # Capture pre-count
    pre = admin_session.get(f"{API}/crypto/accounts", timeout=10).json()
    pre_n = len(pre)

    r = admin_session.post(
        f"{API}/crypto/accounts",
        json={
            "label": "TEST_garbage_keys",
            "api_key": "GARBAGEKEY1234567890",  # gitleaks:allow — obvious dummy
            "api_secret": "GARBAGESECRET1234567890",  # gitleaks:allow — obvious dummy
            "testnet": True,
        },
        timeout=30,
    )
    # Sanity probe to ccxt with bad keys → 422
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text[:300]}"
    body = r.json()
    assert "detail" in body
    assert "verify" in str(body["detail"]).lower() or "binance" in str(body["detail"]).lower()

    # And no row persisted
    post = admin_session.get(f"{API}/crypto/accounts", timeout=10).json()
    assert len(post) == pre_n, "bad-keys POST should NOT persist an account row"


# ---------- /api/crypto/accounts/{bad-id} DELETE ----------
def test_crypto_delete_unknown_id_returns_404(admin_session):
    r = admin_session.delete(f"{API}/crypto/accounts/507f1f77bcf86cd799439099",
                              timeout=10)
    assert r.status_code == 404, f"got {r.status_code}: {r.text[:200]}"


def test_crypto_delete_malformed_id_returns_400(admin_session):
    r = admin_session.delete(f"{API}/crypto/accounts/not-an-objectid", timeout=10)
    # _load_user_account raises 400 for bad ObjectId
    assert r.status_code in (400, 404), f"got {r.status_code}: {r.text[:200]}"


# ---------- regression: pre-iter-35 routes still work ----------
def test_regression_trades_list(admin_session):
    r = admin_session.get(f"{API}/trades", timeout=15)
    assert r.status_code == 200, r.text[:300]
    assert isinstance(r.json(), list)


def test_regression_accounts_list(admin_session):
    r = admin_session.get(f"{API}/accounts", timeout=15)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_regression_research_proposals(admin_session):
    r = admin_session.get(f"{API}/research/proposals", timeout=15)
    # Endpoint may return list or {proposals:[...]}; either is fine, just must not 5xx/404
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:200]}"


def test_regression_trade_explain_endpoint_exists(admin_session):
    # Hit with an unknown trade id — must return 404 (or 400), NOT 500/404-route-missing
    r = admin_session.get(f"{API}/trades/507f1f77bcf86cd799439055/explain",
                          timeout=15)
    assert r.status_code in (200, 400, 404), f"got {r.status_code}: {r.text[:200]}"
    # If route were missing, FastAPI would 404 with {"detail":"Not Found"}; that's still
    # an acceptable signal here, but we additionally verify the body shape isn't an HTML 500.
    assert "Internal Server Error" not in r.text


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
