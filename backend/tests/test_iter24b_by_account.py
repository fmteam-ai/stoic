"""Iter 24b — Per-account performance comparison endpoint.

Contracts:
  - GET /api/analytics/by-account
  - returns {accounts: [...], totals: {...}}
  - one row per MT5 account, sorted by total_pnl descending
  - includes bot_config snapshot (risk, preset, max_lot_size, override flag)
  - profit_factor is None when there are no losses (avoids div-by-zero)
"""
import os
import pathlib
import requests
import pytest

_FRONT_ENV = pathlib.Path("/app/frontend/.env")


def _read_frontend_backend_url() -> str:
    if not _FRONT_ENV.exists():
        return ""
    for line in _FRONT_ENV.read_text().splitlines():
        if line.startswith("REACT_APP_BACKEND_URL="):
            return line.split("=", 1)[1].strip()
    return ""


BASE_URL = (os.environ.get("REACT_APP_BACKEND_URL")
            or _read_frontend_backend_url()
            or "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=15)
    assert r.status_code == 200
    return s


class TestByAccountAnalytics:
    def test_endpoint_authenticated(self, admin_session):
        r = admin_session.get(f"{API}/analytics/by-account", timeout=15)
        assert r.status_code == 200, r.text

    def test_response_shape(self, admin_session):
        r = admin_session.get(f"{API}/analytics/by-account", timeout=15)
        body = r.json()
        assert "accounts" in body
        assert "totals" in body
        assert isinstance(body["accounts"], list)
        assert "accounts" in body["totals"]
        assert "closed_trades" in body["totals"]
        assert "total_pnl" in body["totals"]
        assert "pnl_30d" in body["totals"]

    def test_account_row_fields(self, admin_session):
        r = admin_session.get(f"{API}/analytics/by-account", timeout=15)
        body = r.json()
        if not body["accounts"]:
            pytest.skip("admin has no MT5 accounts")
        row = body["accounts"][0]
        for k in ("account_id", "label", "broker", "mode", "balance",
                  "closed_trades", "open_trades", "pending_trades",
                  "wins", "losses", "win_rate", "total_pnl", "pnl_30d",
                  "avg_pnl", "profit_factor", "active_preset",
                  "risk_level", "max_lot_size", "bot_active",
                  "has_override"):
            assert k in row, f"missing field: {k}"

    def test_sorted_by_total_pnl_desc(self, admin_session):
        r = admin_session.get(f"{API}/analytics/by-account", timeout=15)
        body = r.json()
        pnls = [a["total_pnl"] for a in body["accounts"]]
        assert pnls == sorted(pnls, reverse=True)

    def test_unauthenticated_blocked(self):
        r = requests.get(f"{API}/analytics/by-account", timeout=10)
        assert r.status_code == 401
