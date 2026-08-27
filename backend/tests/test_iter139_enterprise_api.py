"""iter-139 · Phase H Part C — Enterprise API layer tests.

Covers:
  - mgmt (cookie session + X-CSRF-Token): create/list/revoke, validation edges,
    active-key cap (10), invalid scope, empty name, no full-key/hash leak in list
  - public v1 (X-API-Key): /me, /accounts (no secret leak), /trades (filters,
    pagination), /portfolio; missing/malformed/revoked key -> 401;
    missing scope -> 403 on scoped route but 200 on introspection
  - usage tracking: last_used_at + total_requests
  - rate limit: rate_limit_per_minute=5 → 6th call returns 429

Cleanup: every key this suite creates is revoked in teardown so the admin
account is left with no ACTIVE keys (pre-existing revoked ones untouched).
"""
import os
import time
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"


def _login():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW}, timeout=15)
    assert r.status_code == 200, f"login failed {r.status_code} {r.text}"
    csrf = s.cookies.get("csrf_token")
    assert csrf, "csrf_token cookie not set on login"
    s.headers.update({"X-CSRF-Token": csrf})
    return s


@pytest.fixture(scope="module")
def admin():
    s = _login()
    created_ids: list[str] = []
    yield s, created_ids
    # Cleanup: revoke everything this run created
    for kid in created_ids:
        try:
            s.post(f"{BASE_URL}/api/api-keys/{kid}/revoke", timeout=10)
        except Exception:
            pass


def _create_key(session, created_ids, **overrides):
    payload = {
        "name": overrides.get("name", "TEST_ent_key"),
        "scopes": overrides.get("scopes",
                                ["read:accounts", "read:trades", "read:portfolio"]),
        "rate_limit_per_minute": overrides.get("rate_limit_per_minute", 120),
    }
    r = session.post(f"{BASE_URL}/api/api-keys", json=payload, timeout=15)
    if r.status_code == 200:
        created_ids.append(r.json()["record"]["id"])
    return r


# ------------------------------------------------------------- management
class TestApiKeyMgmt:
    def test_create_returns_full_key_once(self, admin):
        s, created = admin
        r = _create_key(s, created, name="TEST_create_full_key")
        assert r.status_code == 200, r.text
        data = r.json()
        assert "api_key" in data and data["api_key"].startswith("stoic_live_"), data
        # length: prefix (11) + 43-ish urlsafe → at least 30 chars
        assert len(data["api_key"]) > 30
        rec = data["record"]
        assert rec["name"] == "TEST_create_full_key"
        assert rec["key_prefix"].startswith("stoic_live_")
        assert data["api_key"].startswith(rec["key_prefix"])
        assert sorted(rec["scopes"]) == ["read:accounts", "read:portfolio", "read:trades"]
        assert rec["revoked_at"] is None
        assert rec["total_requests"] == 0
        # no hash/full key leak in record
        assert "key_hash" not in rec
        assert "api_key" not in rec

    def test_list_never_exposes_full_key_or_hash(self, admin):
        s, _ = admin
        r = s.get(f"{BASE_URL}/api/api-keys", timeout=10)
        assert r.status_code == 200
        rows = r.json()
        assert isinstance(rows, list)
        for row in rows:
            assert "key_hash" not in row
            assert "api_key" not in row
            # only display prefix (short) — never the full 40+ char key
            assert row["key_prefix"].startswith("stoic_live_")
            assert len(row["key_prefix"]) <= 25

    def test_invalid_scope_400(self, admin):
        s, created = admin
        r = _create_key(s, created, name="TEST_bad_scope",
                        scopes=["read:accounts", "write:everything"])
        assert r.status_code == 400, r.text
        assert "scope" in r.text.lower()

    def test_empty_name_422(self, admin):
        s, created = admin
        r = _create_key(s, created, name="")
        assert r.status_code == 422, r.text

    def test_revoke_then_revoke_again_404(self, admin):
        s, created = admin
        r = _create_key(s, created, name="TEST_revoke_twice")
        kid = r.json()["record"]["id"]
        r1 = s.post(f"{BASE_URL}/api/api-keys/{kid}/revoke", timeout=10)
        assert r1.status_code == 200
        r2 = s.post(f"{BASE_URL}/api/api-keys/{kid}/revoke", timeout=10)
        assert r2.status_code == 404, r2.text

    def test_active_key_cap_10(self, admin):
        s, created = admin
        # Read current active count
        rows = s.get(f"{BASE_URL}/api/api-keys", timeout=10).json()
        active = [r for r in rows if not r["revoked_at"]]
        room = 10 - len(active)
        # Fill up to 10
        newly = []
        for i in range(max(room, 0)):
            r = _create_key(s, created, name=f"TEST_cap_{i}")
            assert r.status_code == 200, r.text
            newly.append(r.json()["record"]["id"])
        # 11th should fail with 400
        r = _create_key(s, created, name="TEST_cap_overflow")
        assert r.status_code == 400, r.text
        assert "limit" in r.text.lower() or "10" in r.text
        # Revoke the ones we just filled (leave overall state as we found it)
        for kid in newly:
            s.post(f"{BASE_URL}/api/api-keys/{kid}/revoke", timeout=10)


# --------------------------------------------------------------- public v1
@pytest.fixture(scope="module")
def full_scope_key(admin):
    s, created = admin
    r = _create_key(s, created, name="TEST_full_scope",
                    scopes=["read:accounts", "read:trades", "read:portfolio"],
                    rate_limit_per_minute=1000)
    assert r.status_code == 200, r.text
    return r.json()["api_key"], r.json()["record"]


@pytest.fixture(scope="module")
def trades_only_key(admin):
    s, created = admin
    r = _create_key(s, created, name="TEST_trades_only",
                    scopes=["read:trades"], rate_limit_per_minute=1000)
    assert r.status_code == 200, r.text
    return r.json()["api_key"], r.json()["record"]


class TestPublicV1:
    def test_missing_x_api_key_401(self):
        r = requests.get(f"{BASE_URL}/api/v1/me", timeout=10)
        assert r.status_code == 401

    def test_malformed_key_401(self):
        r = requests.get(f"{BASE_URL}/api/v1/me",
                         headers={"X-API-Key": "not_a_real_key"}, timeout=10)
        assert r.status_code == 401

    def test_wrong_but_prefix_shaped_key_401(self):
        r = requests.get(f"{BASE_URL}/api/v1/me",
                         headers={"X-API-Key": "stoic_live_DEADBEEFdeadbeefdeadbeefdeadbeef"}, timeout=10)
        assert r.status_code == 401

    def test_me_ok(self, full_scope_key):
        key, rec = full_scope_key
        r = requests.get(f"{BASE_URL}/api/v1/me",
                         headers={"X-API-Key": key}, timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["key_prefix"] == rec["key_prefix"]
        assert body["name"] == rec["name"]
        assert sorted(body["scopes"]) == ["read:accounts", "read:portfolio", "read:trades"]
        assert body["rate_limit_per_minute"] == 1000

    def test_accounts_ok_and_no_secret_leak(self, full_scope_key):
        key, _ = full_scope_key
        r = requests.get(f"{BASE_URL}/api/v1/accounts",
                         headers={"X-API-Key": key}, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "accounts" in body and isinstance(body["accounts"], list)
        forbidden = {"bridge_token", "password", "creds", "credentials",
                     "api_key", "secret", "webhook_secret"}
        for a in body["accounts"]:
            leaks = forbidden & set(a.keys())
            assert not leaks, f"account exposed secret fields: {leaks} in {a}"
            # id is a string, not ObjectId
            assert isinstance(a["id"], str)

    def test_trades_pagination_and_filters(self, full_scope_key):
        key, _ = full_scope_key
        h = {"X-API-Key": key}
        r = requests.get(f"{BASE_URL}/api/v1/trades?limit=5&offset=0",
                         headers=h, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert "total" in body and "limit" in body and "offset" in body
        assert body["limit"] == 5 and body["offset"] == 0
        assert isinstance(body["trades"], list)
        assert len(body["trades"]) <= 5
        # limit clamp at 500
        r2 = requests.get(f"{BASE_URL}/api/v1/trades?limit=99999",
                          headers=h, timeout=15)
        assert r2.json()["limit"] == 500
        # status filter smoke: closed only
        r3 = requests.get(f"{BASE_URL}/api/v1/trades?status=closed&limit=10",
                          headers=h, timeout=15)
        assert r3.status_code == 200
        for t in r3.json()["trades"]:
            assert t["status"] == "closed"

    def test_portfolio_shape(self, full_scope_key):
        key, _ = full_scope_key
        r = requests.get(f"{BASE_URL}/api/v1/portfolio",
                         headers={"X-API-Key": key}, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        # accounts_overview returns totals and accounts list
        assert "accounts" in body

    def test_scope_enforcement(self, trades_only_key):
        key, _ = trades_only_key
        h = {"X-API-Key": key}
        # scoped endpoints w/o read:accounts must 403
        r_acc = requests.get(f"{BASE_URL}/api/v1/accounts", headers=h, timeout=10)
        assert r_acc.status_code == 403, r_acc.text
        r_pf = requests.get(f"{BASE_URL}/api/v1/portfolio", headers=h, timeout=10)
        assert r_pf.status_code == 403, r_pf.text
        # trades allowed
        r_tr = requests.get(f"{BASE_URL}/api/v1/trades?limit=1", headers=h, timeout=10)
        assert r_tr.status_code == 200, r_tr.text
        # /me works with any active key
        r_me = requests.get(f"{BASE_URL}/api/v1/me", headers=h, timeout=10)
        assert r_me.status_code == 200

    def test_usage_tracking(self, admin):
        s, created = admin
        r = _create_key(s, created, name="TEST_usage",
                        scopes=["read:accounts"], rate_limit_per_minute=1000)
        key = r.json()["api_key"]
        kid = r.json()["record"]["id"]

        # baseline
        rows = s.get(f"{BASE_URL}/api/api-keys", timeout=10).json()
        row = next(r for r in rows if r["id"] == kid)
        assert row["total_requests"] == 0
        assert row["last_used_at"] is None

        # 3 valid calls
        for _ in range(3):
            requests.get(f"{BASE_URL}/api/v1/me",
                         headers={"X-API-Key": key}, timeout=10)

        rows = s.get(f"{BASE_URL}/api/api-keys", timeout=10).json()
        row = next(r for r in rows if r["id"] == kid)
        assert row["total_requests"] >= 3, row
        assert row["last_used_at"], "last_used_at not set after calls"

    def test_revoked_key_401(self, admin):
        s, created = admin
        r = _create_key(s, created, name="TEST_revoke_401",
                        scopes=["read:accounts"], rate_limit_per_minute=100)
        key = r.json()["api_key"]
        kid = r.json()["record"]["id"]
        # works pre-revoke
        r1 = requests.get(f"{BASE_URL}/api/v1/me",
                          headers={"X-API-Key": key}, timeout=10)
        assert r1.status_code == 200
        # revoke
        rv = s.post(f"{BASE_URL}/api/api-keys/{kid}/revoke", timeout=10)
        assert rv.status_code == 200
        # 401 now
        r2 = requests.get(f"{BASE_URL}/api/v1/me",
                          headers={"X-API-Key": key}, timeout=10)
        assert r2.status_code == 401


class TestRateLimit:
    def test_429_on_6th_call_with_limit_5(self, admin):
        s, created = admin
        r = _create_key(s, created, name="TEST_ratelimit_5",
                        scopes=["read:accounts"], rate_limit_per_minute=5)
        key = r.json()["api_key"]
        h = {"X-API-Key": key}
        statuses = []
        for i in range(6):
            resp = requests.get(f"{BASE_URL}/api/v1/me", headers=h, timeout=10)
            statuses.append(resp.status_code)
            # Very short pause to not race
            time.sleep(0.05)
        assert statuses[:5] == [200, 200, 200, 200, 200], statuses
        assert statuses[5] == 429, statuses


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
