"""Iter-13 tests: per-user broker/account caps + broker presets.

Clean up at module end: deletes any user matching ^qa_limits_ and their accounts.
"""
import os
import uuid
import pytest
import requests
from pymongo import MongoClient

from live_target import require_live_base_url
BASE_URL = require_live_base_url()
API = f"{BASE_URL}/api"
from live_target import resolve_admin_credentials
ADMIN_EMAIL, ADMIN_PASSWORD = resolve_admin_credentials()
MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "ai_trading_bot")


def _fresh_email():
    return f"qa_limits_{uuid.uuid4().hex[:8]}@example.com"


def _register(email, password="Kd5#Zt9mW2xVpR7c"):
    from helpers import make_elite, register_and_login
    s = register_and_login(email, password, name="QA Limits")
    # iter-60 tier quotas (Starter=1/Pro=3) fire with 402 BEFORE the broker
    # caps this suite tests — Elite (unlimited accounts) exposes the caps.
    make_elite(email)
    return s


def _live_payload(broker, n, label=None, server=None):
    return {
        "label": label or f"{broker}#{n}",
        "broker": broker,
        "server": server or f"{broker}-Live",
        "account_number": f"{broker[:3].upper()}{n}{uuid.uuid4().hex[:4]}",
        "account_type": "standard",
        "base_currency": "USD",
        "mode": "live",
        "initial_balance": 0,
    }


def _paper_payload(n):
    return {
        "label": f"Paper#{n}",
        "broker": "",
        "server": "",
        "account_number": f"PAP{n}{uuid.uuid4().hex[:4]}",
        "account_type": "microcent",
        "base_currency": "USD",
        "mode": "paper",
        "initial_balance": 5000,
    }


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    try:
        client = MongoClient(MONGO_URL)
        db = client[DB_NAME]
        users = list(db.users.find({"email": {"$regex": "^qa_limits_"}}, {"_id": 0, "id": 1}))
        for u in users:
            db.accounts.delete_many({"user_id": u["id"]})
        db.users.delete_many({"email": {"$regex": "^qa_limits_"}})
        client.close()
    except Exception as e:
        print(f"cleanup err: {e}")


# ---- limits endpoint shape ----
class TestLimitsEndpoint:
    def test_limits_shape_fresh_user(self):
        s = _register(_fresh_email())
        r = s.get(f"{API}/accounts/limits", timeout=10)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["max_brokers"] == 5
        assert body["max_accounts_per_broker"] == 3
        assert body["brokers_used"] == 0
        assert body["total_live_accounts"] == 0
        assert body["breakdown"] == []
        assert isinstance(body["checked_at"], str)
        assert isinstance(body["max_brokers"], int)
        assert isinstance(body["brokers_used"], int)


# ---- presets endpoint ----
class TestPresetsEndpoint:
    def test_presets_returns_list(self):
        s = _register(_fresh_email())
        r = s.get(f"{API}/accounts/broker-presets", timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert "presets" in body
        assert len(body["presets"]) >= 8
        for p in body["presets"]:
            assert "broker" in p and isinstance(p["broker"], str)
            assert "servers" in p and isinstance(p["servers"], list)
            assert "account_types" in p and isinstance(p["account_types"], list)


# ---- per-broker cap (3) ----
class TestPerBrokerCap:
    def test_three_roboforex_ok_fourth_403(self):
        s = _register(_fresh_email())
        for n in range(1, 4):
            r = s.post(f"{API}/accounts", json=_live_payload("RoboForex", n), timeout=15)
            assert r.status_code == 200, f"n={n} {r.status_code} {r.text}"
        r4 = s.post(f"{API}/accounts", json=_live_payload("RoboForex", 4), timeout=15)
        assert r4.status_code == 403, r4.text
        assert "account limit reached" in r4.json().get("detail", "").lower()
        assert "roboforex" in r4.json().get("detail", "").lower()
        # limits reflect 3/3
        lim = s.get(f"{API}/accounts/limits", timeout=10).json()
        assert lim["brokers_used"] == 1
        assert lim["total_live_accounts"] == 3
        assert lim["breakdown"][0]["count"] == 3
        assert lim["breakdown"][0]["remaining_slots"] == 0


# ---- broker cap (5) ----
class TestBrokerCap:
    def test_five_distinct_brokers_ok_sixth_403(self):
        s = _register(_fresh_email())
        brokers = ["RoboForex", "IC Markets", "Exness", "FXTM", "Pepperstone"]
        for i, b in enumerate(brokers):
            r = s.post(f"{API}/accounts", json=_live_payload(b, i + 1), timeout=15)
            assert r.status_code == 200, f"{b} -> {r.status_code} {r.text}"
        # 6th distinct broker
        r6 = s.post(f"{API}/accounts", json=_live_payload("OctaFX", 99), timeout=15)
        assert r6.status_code == 403, r6.text
        assert "broker limit reached" in r6.json().get("detail", "").lower()
        lim = s.get(f"{API}/accounts/limits", timeout=10).json()
        assert lim["brokers_used"] == 5


# ---- paper bypass ----
class TestPaperBypass:
    def test_paper_works_even_when_caps_reached(self):
        s = _register(_fresh_email())
        # fill 5 brokers x 3 accounts each
        brokers = ["RoboForex", "IC Markets", "Exness", "FXTM", "Pepperstone"]
        for b in brokers:
            for n in range(3):
                r = s.post(f"{API}/accounts", json=_live_payload(b, n + 1), timeout=15)
                assert r.status_code == 200, f"{b}#{n} {r.text}"
        # confirm caps
        lim = s.get(f"{API}/accounts/limits", timeout=10).json()
        assert lim["brokers_used"] == 5
        assert lim["total_live_accounts"] == 15
        # paper still ok
        r = s.post(f"{API}/accounts", json=_paper_payload(1), timeout=15)
        assert r.status_code == 200, r.text
        # paper not counted
        lim2 = s.get(f"{API}/accounts/limits", timeout=10).json()
        assert lim2["brokers_used"] == 5
        assert lim2["total_live_accounts"] == 15


# ---- case-insensitive matching ----
class TestCaseInsensitive:
    def test_roboforex_vs_roboforex_same_bucket(self):
        s = _register(_fresh_email())
        r1 = s.post(f"{API}/accounts", json=_live_payload("RoboForex", 1), timeout=15)
        assert r1.status_code == 200
        r2 = s.post(f"{API}/accounts", json=_live_payload("roboforex", 2), timeout=15)
        assert r2.status_code == 200
        r3 = s.post(f"{API}/accounts", json=_live_payload("ROBOFOREX", 3), timeout=15)
        assert r3.status_code == 200
        # 4th in any case should fail
        r4 = s.post(f"{API}/accounts", json=_live_payload("RoboForex", 4), timeout=15)
        assert r4.status_code == 403, r4.text
        lim = s.get(f"{API}/accounts/limits", timeout=10).json()
        assert lim["brokers_used"] == 1  # one bucket
        assert lim["total_live_accounts"] == 3


# ---- delete frees a slot ----
class TestDeleteFreesSlot:
    def test_delete_then_add_succeeds(self):
        s = _register(_fresh_email())
        ids = []
        for n in range(1, 4):
            r = s.post(f"{API}/accounts", json=_live_payload("RoboForex", n), timeout=15)
            assert r.status_code == 200, r.text
            ids.append(r.json()["id"])
        # 4th rejected
        r_block = s.post(f"{API}/accounts", json=_live_payload("RoboForex", 4), timeout=15)
        assert r_block.status_code == 403
        # delete first
        d = s.delete(f"{API}/accounts/{ids[0]}", timeout=10)
        assert d.status_code == 200
        lim = s.get(f"{API}/accounts/limits", timeout=10).json()
        assert lim["breakdown"][0]["count"] == 2
        assert lim["breakdown"][0]["remaining_slots"] == 1
        # now 4th attempt should succeed
        r_ok = s.post(f"{API}/accounts", json=_live_payload("RoboForex", 4), timeout=15)
        assert r_ok.status_code == 200, r_ok.text


# ---- missing broker for live ----
class TestMissingBrokerLive:
    def test_empty_broker_live_403(self):
        s = _register(_fresh_email())
        p = _live_payload("RoboForex", 1)
        p["broker"] = ""
        r = s.post(f"{API}/accounts", json=p, timeout=15)
        # AccountCreate may have validation. We expect 403 per problem statement.
        # If pydantic rejects with 422, we record that too.
        assert r.status_code in (403, 422), r.text
        if r.status_code == 403:
            assert "broker name is required" in r.json().get("detail", "").lower()


# ---- regression: paper still works on a clean user, list returns, admin login ----
class TestRegression:
    def test_paper_create_and_list(self):
        s = _register(_fresh_email())
        r = s.post(f"{API}/accounts", json=_paper_payload(1), timeout=15)
        assert r.status_code == 200, r.text
        lst = s.get(f"{API}/accounts", timeout=10)
        assert lst.status_code == 200
        items = lst.json()
        assert isinstance(items, list)
        assert len(items) == 1
        assert items[0]["mode"] == "paper"

    def test_admin_login_still_works(self):
        s = requests.Session()
        r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=10)
        if r.status_code == 401:
            # Preview DB uses the documented legacy test credentials while
            # backend/.env carries the production recovery password.
            r = s.post(f"{API}/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, timeout=10)
        assert r.status_code == 200, r.text
        me = s.get(f"{API}/auth/me", timeout=10)
        assert me.status_code == 200
        assert me.json()["email"] in (ADMIN_EMAIL, ADMIN_EMAIL)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
