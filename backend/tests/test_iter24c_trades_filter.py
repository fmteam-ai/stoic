"""Iter 24c — Trades + stats filterable by account_id.

Contracts:
  - GET /api/trades?account_id=X    -> only that account's trades
  - GET /api/trades/stats?account_id=X -> stats narrowed to that account
  - omitting account_id keeps the all-accounts behaviour
  - Unknown ids return an empty list (no 404 — pagination-style filter)
"""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import os
import pathlib
import pytest
import requests

_FRONT_ENV = pathlib.Path(_os.path.join(_REPO_DIR, "frontend", ".env"))


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


@pytest.fixture(scope="module")
def accounts(admin_session):
    return admin_session.get(f"{API}/accounts", timeout=10).json()


class TestTradesByAccount:
    def test_list_trades_no_filter_returns_all(self, admin_session):
        r = admin_session.get(f"{API}/trades?limit=500", timeout=15)
        assert r.status_code == 200
        ts = r.json()
        assert isinstance(ts, list)

    def test_filter_by_account_id(self, admin_session, accounts):
        if len(accounts) < 1:
            pytest.skip("admin has no accounts")
        acc_id = accounts[0]["id"]
        r = admin_session.get(f"{API}/trades?limit=500&account_id={acc_id}", timeout=15)
        assert r.status_code == 200
        ts = r.json()
        # Every returned trade must belong to the requested account
        for t in ts:
            assert t.get("account_id") == acc_id

    def test_filter_partition_sums(self, admin_session, accounts):
        """The sum of per-account trade counts must equal the all-accounts count
        (modulo the optional `unassigned` bucket for legacy trades without account_id).
        """
        if len(accounts) < 2:
            pytest.skip("need 2+ accounts for partition test")
        all_trades = admin_session.get(f"{API}/trades?limit=10000", timeout=15).json()
        per_account_sum = 0
        for a in accounts:
            r = admin_session.get(f"{API}/trades?limit=10000&account_id={a['id']}", timeout=15)
            per_account_sum += len(r.json())
        # Trades without an account_id — or pointing at a since-deleted
        # account (QA cleanup leaves orphans) — are dropped by the
        # per-account filter; allow that gap.
        acct_ids = {a["id"] for a in accounts}
        unassigned = sum(1 for t in all_trades
                         if t.get("account_id") not in acct_ids)
        assert per_account_sum + unassigned == len(all_trades)

    def test_stats_filtered_by_account(self, admin_session, accounts):
        if not accounts:
            pytest.skip("admin has no accounts")
        acc_id = accounts[0]["id"]
        r = admin_session.get(f"{API}/trades/stats?account_id={acc_id}", timeout=10)
        assert r.status_code == 200
        body = r.json()
        for k in ("total_trades", "win_rate", "total_pnl", "wins", "losses",
                  "open_trades"):
            assert k in body
        # Sanity: per-account stats can't exceed global ones
        global_stats = admin_session.get(f"{API}/trades/stats", timeout=10).json()
        assert body["total_trades"] <= global_stats["total_trades"]
        assert body["open_trades"] <= global_stats["open_trades"]

    def test_live_filtered_by_account(self, admin_session, accounts):
        if not accounts:
            pytest.skip("admin has no accounts")
        acc_id = accounts[0]["id"]
        r = admin_session.get(f"{API}/trades/live?account_id={acc_id}", timeout=15)
        assert r.status_code == 200
        for t in r.json():
            assert t.get("account_id") == acc_id


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
