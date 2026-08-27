"""iter-157 — Verified Live Performance + Audit Log endpoints.

Covers backend acceptance criteria:
  B1: GET /api/performance/verified as admin
  B2: share lifecycle (create → public GET → rotate → public GET (old) 404 → delete → 404)
  B3: GET /api/auth/audit?limit=200 as admin
"""
import os
import requests
import pytest

BASE = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE}/api"

ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"admin login failed: {r.status_code} {r.text[:200]}"
    return s


# --- B1: verified track record --------------------------------------------
class TestVerifiedPerformance:
    def test_verified_shape(self, admin_session):
        r = admin_session.get(f"{API}/performance/verified")
        assert r.status_code == 200, r.text[:200]
        d = r.json()
        # top-level keys
        for k in ("overall", "max_drawdown", "equity_curve", "accounts", "integrity"):
            assert k in d, f"missing key {k}"
        # overall stats
        o = d["overall"]
        for k in ("net_pnl", "deals", "closed_positions", "win_rate"):
            assert k in o, f"overall missing {k}"
        assert isinstance(o["deals"], int) and o["deals"] > 0
        assert o["net_pnl"] is not None
        # equity curve
        assert isinstance(d["equity_curve"], list) and len(d["equity_curve"]) > 0
        pt = d["equity_curve"][0]
        assert set(("date", "net", "cum")).issubset(pt.keys())
        # accounts
        assert isinstance(d["accounts"], list) and len(d["accounts"]) > 0
        # integrity
        integ = d["integrity"]
        assert integ["source"] == "broker_deals"
        assert "closed_trades" in integ and "verified_pct" in integ
        # share field present (may be null or object)
        assert "share" in d

    def test_expected_totals_admin(self, admin_session):
        """Spec says overall net_pnl≈6910, deals≈2262, win_rate≈68. Allow ±5% drift."""
        d = admin_session.get(f"{API}/performance/verified").json()
        o = d["overall"]
        assert abs(o["net_pnl"] - 6910) < 400, f"net_pnl={o['net_pnl']} not near 6910"
        assert abs(o["deals"] - 2262) < 200, f"deals={o['deals']} not near 2262"
        assert o["win_rate"] is not None
        assert abs(o["win_rate"] - 68) < 10, f"win_rate={o['win_rate']} not near 68"
        integ = d["integrity"]
        assert integ["closed_trades"] >= 300, f"closed_trades={integ['closed_trades']}"
        if integ["verified_pct"] is not None:
            assert integ["verified_pct"] >= 95, f"verified_pct={integ['verified_pct']}"


# --- B2: share lifecycle ---------------------------------------------------
class TestShareLifecycle:
    def test_share_create_rotate_public_and_revoke(self, admin_session):
        # start clean
        admin_session.delete(f"{API}/performance/share")

        # 1. create share
        r = admin_session.post(f"{API}/performance/share")
        assert r.status_code == 200
        sid1 = r.json()["share_id"]
        assert sid1 and len(sid1) > 10

        # 2. public GET (unauthenticated context)
        pub = requests.Session()
        r = pub.get(f"{API}/public/performance/{sid1}")
        assert r.status_code == 200
        d = r.json()
        assert d.get("shared") is True
        assert "accounts" in d and len(d["accounts"]) > 0
        # masked labels: ACCOUNT-N pattern
        for a in d["accounts"]:
            assert a["label"].startswith("ACCOUNT-"), f"unmasked label: {a['label']}"
        assert "overall" in d and "integrity" in d and "equity_curve" in d

        # 3. rotate — new POST returns new id, and old id 404s
        r = admin_session.post(f"{API}/performance/share")
        assert r.status_code == 200
        sid2 = r.json()["share_id"]
        assert sid2 != sid1
        r_old = pub.get(f"{API}/public/performance/{sid1}")
        assert r_old.status_code == 404, f"old id still resolves: {r_old.status_code}"
        r_new = pub.get(f"{API}/public/performance/{sid2}")
        assert r_new.status_code == 200

        # 4. verified endpoint should show current share
        v = admin_session.get(f"{API}/performance/verified").json()
        assert v["share"] is not None
        assert v["share"]["share_id"] == sid2

        # 5. revoke → new id 404s
        r = admin_session.delete(f"{API}/performance/share")
        assert r.status_code == 200
        r_after = pub.get(f"{API}/public/performance/{sid2}")
        assert r_after.status_code == 404
        v2 = admin_session.get(f"{API}/performance/verified").json()
        assert v2["share"] is None

    def test_bogus_share_id_404(self):
        pub = requests.Session()
        r = pub.get(f"{API}/public/performance/not-a-real-share-id-xxxxx")
        assert r.status_code == 404

    def test_public_endpoint_needs_no_auth(self, admin_session):
        # create then verify with a completely new session (no cookies)
        admin_session.delete(f"{API}/performance/share")
        sid = admin_session.post(f"{API}/performance/share").json()["share_id"]
        try:
            fresh = requests.Session()  # no auth
            r = fresh.get(f"{API}/public/performance/{sid}")
            assert r.status_code == 200
            assert r.json().get("shared") is True
        finally:
            admin_session.delete(f"{API}/performance/share")


# --- B3: audit log ---------------------------------------------------------
class TestAuditLog:
    def test_audit_log_returns_list(self, admin_session):
        r = admin_session.get(f"{API}/auth/audit?limit=200")
        assert r.status_code == 200, r.text[:200]
        items = r.json()
        assert isinstance(items, list)
        # allow empty in a fresh env, but the spec says ~33 events
        for e in items[:5]:
            assert "action" in e
            assert "at" in e
            # step_up_verified may or may not be set; if present, boolean
            if "step_up_verified" in e:
                assert isinstance(e["step_up_verified"], bool)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
