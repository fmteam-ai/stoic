from live_target import ADMIN_EMAIL, ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""HTTP API tests for STOIC Brain Phase A endpoints."""
import os
import requests
import pytest

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") or "http://localhost:3000"
# Backend actually uses REACT_APP_BACKEND_URL from frontend/.env (repo-relative path)
_FRONTEND_ENV = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "frontend", ".env",
)
if os.path.exists(_FRONTEND_ENV):
    with open(_FRONTEND_ENV) as f:
        for line in f:
            if line.startswith("REACT_APP_BACKEND_URL"):
                BASE_URL = line.split("=", 1)[1].strip().rstrip("/")

ADMIN = {"email": "admin@stoicaibot.com", "password": ADMIN_PASSWORD}


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login", json=ADMIN, timeout=15)
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text[:200]}"
    return s


def test_brain_regime(session):
    r = session.get(f"{BASE_URL}/api/brain/regime", params={"symbol": "XAUUSD"}, timeout=15)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert data.get("available") is True, f"unavailable: {data}"
    v = data.get("vector", {})
    expected_dims = {"trend", "volatility", "liquidity", "momentum", "mean_reversion",
                     "correlation_stress", "news_risk", "spread_stress", "gap_risk"}
    missing = expected_dims - set(v.keys())
    assert not missing, f"missing dims: {missing}"
    labels = data.get("labels", [])
    assert isinstance(labels, list) and len(labels) == 5, f"labels={labels}"
    assert data.get("fingerprint_key"), "no fingerprint_key"
    assert "session" in data


def test_brain_router(session):
    r = session.get(f"{BASE_URL}/api/brain/router", params={"symbol": "XAUUSD"}, timeout=15)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    w = data.get("weights", {})
    for fam in ("ai", "scalp_fast", "swing"):
        assert fam in w, f"missing {fam}"
        assert 0.05 - 1e-6 <= w[fam] <= 0.70 + 1e-6, f"{fam}={w[fam]} out of bounds"
    total = sum(w.values())
    assert abs(total - 1.0) < 0.02, f"weights sum={total}"
    assert "evidence" in data
    assert "market_state" in data


def test_brain_meta_recent(session):
    r = session.get(f"{BASE_URL}/api/brain/meta/recent", timeout=15)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert "decisions" in data
    assert isinstance(data["decisions"], list)


def test_brain_portfolio(session):
    acc = session.get(f"{BASE_URL}/api/accounts", timeout=15)
    assert acc.status_code == 200, acc.text[:300]
    accounts = acc.json()
    if isinstance(accounts, dict):
        accounts = accounts.get("accounts") or accounts.get("data") or []
    if not accounts:
        pytest.skip("no accounts available")
    aid = accounts[0].get("account_id") or accounts[0].get("id") or accounts[0].get("_id")
    assert aid, f"no id key in account: {accounts[0]}"
    r = session.get(f"{BASE_URL}/api/brain/portfolio", params={"account_id": aid}, timeout=15)
    assert r.status_code == 200, r.text[:300]
    data = r.json()
    assert "factors" in data, f"no factors key: {list(data.keys())}"
    assert "currency_exposure" in data
    assert "stress" in data
    assert "limits" in data


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
