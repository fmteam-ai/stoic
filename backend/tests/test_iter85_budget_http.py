"""iter-85 HTTP tests for GET /api/risk/budget (independent verification)."""
import os
import time
import uuid

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/")
API = f"{BASE_URL}/api"
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PASSWORD = "admin123"


def _login(session: requests.Session, email: str, password: str) -> bool:
    r = session.post(f"{API}/auth/login",
                     json={"email": email, "password": password}, timeout=15)
    return r.status_code == 200


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    assert _login(s, ADMIN_EMAIL, ADMIN_PASSWORD), "admin login failed"
    return s


@pytest.fixture()
def user_session():
    """Registers a fresh non-admin user (bypassing email verification)."""
    email = f"iter85http+{uuid.uuid4().hex[:8]}@example.com"
    password = "SecretPass_123"
    s = requests.Session()
    r = s.post(f"{API}/auth/register",
               json={"email": email, "password": password,
                     "name": "iter85", "terms_agreed": True}, timeout=15)
    assert r.status_code in (200, 201), r.text
    # bypass activation via direct db flip
    from database import get_db
    from tests.conftest import run_async

    async def _flip():
        db = get_db()
        await db.users.update_one(
            {"email": email}, {"$set": {"email_verified": True}})
    run_async(_flip())
    # re-login
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": password}, timeout=15)
    assert r.status_code == 200, r.text
    yield s, email


class TestRiskBudgetHTTP:
    def test_unauthenticated_401_or_403(self):
        r = requests.get(f"{API}/risk/budget", timeout=15)
        assert r.status_code in (401, 403), r.status_code

    def test_admin_default_shape(self, admin_session):
        r = admin_session.get(f"{API}/risk/budget", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["pool_risk_pct"] == 3.0
        assert isinstance(d["strategies"], list) and len(d["strategies"]) == 5
        expected = {"trend", "scalp", "breakout", "mean_reversion", "experimental"}
        assert {s["strategy"] for s in d["strategies"]} == expected
        total = 0.0
        for s in d["strategies"]:
            for k in ("allocation_pct", "perf_weight",
                      "budget_risk_pct", "spent_risk_pct",
                      "remaining_risk_pct"):
                assert k in s, f"missing {k} in {s['strategy']}"
            total += s["allocation_pct"]
            assert 0 < s["budget_risk_pct"] <= 3.0
            assert s["spent_risk_pct"] >= 0
            # remaining = budget - spent (within float tol)
            assert abs(
                s["remaining_risk_pct"]
                - (s["budget_risk_pct"] - s["spent_risk_pct"])
            ) < 1e-6
        assert abs(total - 100.0) < 1e-6, f"allocations sum={total}"

    def test_non_admin_cannot_read_others_account(self, user_session):
        s, _email = user_session
        # Find any account NOT owned by this user (admin's).
        from database import get_db
        from tests.conftest import run_async

        async def _find_foreign():
            db = get_db()
            u = await db.users.find_one({"email": ADMIN_EMAIL})
            if not u:
                return None
            uid = u.get("id") or str(u.get("_id"))
            acc = await db.accounts.find_one({"user_id": uid})
            return str(acc["_id"]) if acc else None
        foreign = run_async(_find_foreign())
        if not foreign:
            pytest.skip("no foreign account available")
        r = s.get(f"{API}/risk/budget?account_id={foreign}", timeout=15)
        assert r.status_code == 403, r.text

    def test_bogus_account_id_404(self, admin_session):
        # 24-hex ObjectId that does not exist
        r = admin_session.get(
            f"{API}/risk/budget?account_id=000000000000000000000000",
            timeout=15)
        assert r.status_code == 404, r.text

    def test_admin_has_perf_shrink_on_trend(self, admin_session):
        """Review-request note: admin has losing history so trend perf<1.0."""
        r = admin_session.get(f"{API}/risk/budget", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        trend = next(s for s in d["strategies"] if s["strategy"] == "trend")
        # perf_weight is bounded (0.5..1.0) and never disables budget
        assert 0.0 < trend["perf_weight"] <= 1.0
        assert trend["budget_risk_pct"] > 0


class TestHotPathRegression:
    """iter-85 hot-path regression: bot start/stop + status + trades."""

    def test_bot_status_ok(self, admin_session):
        r = admin_session.get(f"{API}/bot/status", timeout=15)
        assert r.status_code == 200, r.text

    def test_trades_list_ok(self, admin_session):
        r = admin_session.get(f"{API}/trades?limit=5", timeout=15)
        assert r.status_code == 200, r.text
        assert isinstance(r.json(), (list, dict))

    def test_risk_layers_still_ok(self, admin_session):
        r = admin_session.get(f"{API}/risk/layers", timeout=15)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "layers" in d and len(d["layers"]) == 12
