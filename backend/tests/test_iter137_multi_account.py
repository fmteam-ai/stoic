"""iter-137 · Multi-account management API tests.

Covers:
  · GET  /api/accounts/overview            — totals + per-account + groups
  · GET  /api/accounts/equity-curve         — daily series w/ per-account keys
  · PATCH /api/accounts/{id}                — label / group / trading_enabled
  · GET  /api/accounts                      — list still exposes new fields
"""
import os
import pytest
import requests
from live_target import require_live_base_url

BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=15)
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text}"
    # Cookie-based csrf token surfaced in cookie 'csrf_token'
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


@pytest.fixture(scope="module")
def accounts(session):
    r = session.get(f"{API}/accounts", timeout=15)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert len(data) >= 2, "Need >= 2 accounts on the admin user"
    return data


# ─────────────────────────────────────────────────────────────
# Overview
# ─────────────────────────────────────────────────────────────
class TestOverview:
    def test_shape(self, session):
        r = session.get(f"{API}/accounts/overview", timeout=15)
        assert r.status_code == 200
        data = r.json()
        assert set(data.keys()) >= {"totals", "accounts", "groups"}
        t = data["totals"]
        for key in ("balance", "equity", "floating", "pnl_today", "pnl_7d",
                    "pnl_30d", "connected", "trading_enabled", "open_positions",
                    "accounts"):
            assert key in t, f"missing totals.{key}"
        assert isinstance(data["groups"], list)
        assert isinstance(data["accounts"], list)
        assert len(data["accounts"]) == t["accounts"]

    def test_per_account_entries(self, session):
        r = session.get(f"{API}/accounts/overview", timeout=15)
        assert r.status_code == 200
        for a in r.json()["accounts"]:
            for k in ("id", "label", "trading_enabled", "connected",
                      "pnl_today", "pnl_7d", "pnl_30d"):
                assert k in a, f"missing per-account key {k}"
            assert isinstance(a["trading_enabled"], bool)
            assert isinstance(a["connected"], bool)

    def test_floating_matches_equity_minus_balance(self, session):
        t = session.get(f"{API}/accounts/overview", timeout=15).json()["totals"]
        assert abs(t["floating"] - (t["equity"] - t["balance"])) < 0.02


# ─────────────────────────────────────────────────────────────
# Equity curve
# ─────────────────────────────────────────────────────────────
class TestEquityCurve:
    @pytest.mark.parametrize("days", [7, 30, 90])
    def test_series_length(self, session, days):
        r = session.get(f"{API}/accounts/equity-curve?days={days}", timeout=15)
        assert r.status_code == 200
        j = r.json()
        assert j["days"] == days
        assert len(j["series"]) == days
        # Every point carries "date" + "total" + a_<id> keys.
        acct_ids = [a["id"] for a in j["accounts"]]
        for p in j["series"]:
            assert "date" in p and "total" in p
            for aid in acct_ids:
                assert f"a_{aid}" in p

    def test_days_clamp_low(self, session):
        r = session.get(f"{API}/accounts/equity-curve?days=0", timeout=15)
        assert r.status_code == 200
        assert r.json()["days"] == 1

    def test_days_clamp_high(self, session):
        r = session.get(f"{API}/accounts/equity-curve?days=999", timeout=15)
        assert r.status_code == 200
        assert r.json()["days"] == 365


# ─────────────────────────────────────────────────────────────
# PATCH /accounts/{id} — group + trading_enabled + validation
# ─────────────────────────────────────────────────────────────
class TestPatchAccount:
    def test_empty_payload_400(self, session, accounts):
        aid = accounts[0]["id"]
        r = session.patch(f"{API}/accounts/{aid}", json={}, timeout=15)
        assert r.status_code == 400, r.text

    def test_empty_label_400(self, session, accounts):
        aid = accounts[0]["id"]
        r = session.patch(f"{API}/accounts/{aid}", json={"label": "   "},
                          timeout=15)
        assert r.status_code == 400, r.text

    def test_bogus_id_404(self, session):
        # Well-formed ObjectId that doesn't belong to anyone
        r = session.patch(f"{API}/accounts/507f1f77bcf86cd799439011",
                          json={"trading_enabled": False}, timeout=15)
        assert r.status_code == 404, r.text

    def test_set_group_trading_and_restore(self, session, accounts):
        aid = accounts[0]["id"]
        acct_num = accounts[0].get("account_number")
        original_group = accounts[0].get("group")
        original_enabled = accounts[0].get("trading_enabled", True)

        # SET: group=TESTGROUP + trading_enabled=False
        r = session.patch(f"{API}/accounts/{aid}",
                          json={"group": "TESTGROUP", "trading_enabled": False},
                          timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["group"] == "TESTGROUP"
        assert body["trading_enabled"] is False
        # Response is _serialized_ (id, no _id)
        assert body.get("id") == aid
        assert "_id" not in body

        # VERIFY via GET /accounts + overview
        listing = session.get(f"{API}/accounts", timeout=15).json()
        row = next(x for x in listing if x["id"] == aid)
        assert row["group"] == "TESTGROUP"
        assert row["trading_enabled"] is False

        ov = session.get(f"{API}/accounts/overview", timeout=15).json()
        ov_row = next(x for x in ov["accounts"] if x["id"] == aid)
        assert ov_row["trading_enabled"] is False
        assert ov_row["group"] == "TESTGROUP"
        assert "TESTGROUP" in ov["groups"]

        # RESTORE — clear group + re-enable trading (per review request)
        restore_payload = {"group": "", "trading_enabled": True}
        # We can also restore the original label if it was somehow mangled
        r2 = session.patch(f"{API}/accounts/{aid}", json=restore_payload,
                           timeout=15)
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["trading_enabled"] is True
        # An empty string group cleared the group — either stored as ""
        # or reported as None. Either way overview should NOT include it.
        assert (body2.get("group") in ("", None))
        # Silence unused variable lint
        _ = (acct_num, original_group, original_enabled)

    def test_restore_all_accounts_clean(self, session, accounts):
        """Belt-and-braces: leave every account with trading ON + group cleared."""
        for a in accounts:
            r = session.patch(f"{API}/accounts/{a['id']}",
                              json={"group": "", "trading_enabled": True},
                              timeout=15)
            assert r.status_code == 200


# ─────────────────────────────────────────────────────────────
# GET /accounts still exposes new fields after PATCH
# ─────────────────────────────────────────────────────────────
class TestListStillWorks:
    def test_list_exposes_fields(self, session, accounts):
        aid = accounts[0]["id"]
        # Set a temp group we can grep for
        session.patch(f"{API}/accounts/{aid}",
                      json={"group": "FIELDCHECK", "trading_enabled": False},
                      timeout=15)
        try:
            data = session.get(f"{API}/accounts", timeout=15).json()
            row = next(x for x in data if x["id"] == aid)
            assert row.get("group") == "FIELDCHECK"
            assert row.get("trading_enabled") is False
        finally:
            # Restore
            session.patch(f"{API}/accounts/{aid}",
                          json={"group": "", "trading_enabled": True},
                          timeout=15)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
