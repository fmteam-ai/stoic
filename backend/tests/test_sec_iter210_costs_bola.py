"""iter-210 SEC — BOLA fix on GET /api/brain/costs?account_id=...

Verifies:
 (a) attacker (user A) with victim (user B) account_id → 404
 (b) owner (user B) → 200 with basis.account_id set
 (c) admin → 200 (admin bypass intentional)
 (d) attacker with random valid ObjectId → 404
 (e) regression: no account_id → 200 for any user
 (f) defence-in-depth: transaction_costs._realized_deal_cost_r
     user_id-scoped so attacker cannot get commission_source=realized_deals
     from victim's deals; owner does get realized_deals.
 (g) decision_events cap: record_stage 45x → count == 40.
"""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import requests
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: F401,E402  # ensures env + indexes
from database import get_db  # noqa: E402
from tests.helpers import (base_url, mark_email_verified,  # noqa: E402
                           mongo_db)

BASE = base_url()
API = f"{BASE}/api"

TAG = f"TEST_iter210_{uuid.uuid4().hex[:8]}"
USER_A_EMAIL = f"{TAG.lower()}-a@qa.example.com"
USER_B_EMAIL = f"{TAG.lower()}-b@qa.example.com"
PW = "Kd5#Zt9mW2xVpR7c"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _register_login(email: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{API}/auth/register",
               json={"email": email, "password": PW, "name": "iter210",
                     "terms_agreed": True}, timeout=30)
    assert r.status_code == 200, f"register: {r.status_code} {r.text}"
    mark_email_verified(email)
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": PW}, timeout=30)
    assert r.status_code == 200, f"login: {r.status_code} {r.text}"
    return s


@pytest.fixture(scope="module")
def user_a():
    return _register_login(USER_A_EMAIL)


@pytest.fixture(scope="module")
def user_b():
    return _register_login(USER_B_EMAIL)


@pytest.fixture(scope="module")
def admin():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@stoicaibot.com", "password": "admin123"},
               timeout=30)
    assert r.status_code == 200, r.text
    return s


@pytest.fixture(scope="module")
def b_account_id(user_b):
    """Seed an accounts doc owned by user B + a handful of broker_deals."""
    m = mongo_db()
    ub = m.users.find_one({"email": USER_B_EMAIL.lower()})
    assert ub, "user B missing"
    b_uid = ub.get("id") or str(ub["_id"])

    ua = m.users.find_one({"email": USER_A_EMAIL.lower()})
    a_uid = ua.get("id") or str(ua["_id"]) if ua else None

    acc_doc = {
        "user_id": b_uid,
        "name": f"{TAG}_acc",
        "broker": "TEST",
        "equity": 10000.0,
        "balance": 10000.0,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # deliberately omit bridge_token (partial unique index)
    }
    ins = m.accounts.insert_one(acc_doc)
    acc_id = str(ins.inserted_id)

    now = datetime.now(timezone.utc)
    deals = [{"account_id": acc_id, "user_id": b_uid, "symbol": "XAUUSD",
              "deal_id": f"{TAG}_deal_{i}", "commission": -7.0, "swap": -2.0,
              "lots": 1.0,
              "deal_time": (now - timedelta(hours=i)).isoformat()}
             for i in range(6)]
    m.broker_deals.insert_many(deals)

    yield {"account_id": acc_id, "b_uid": b_uid, "a_uid": a_uid}

    # cleanup
    m.accounts.delete_one({"_id": ObjectId(acc_id)})
    m.broker_deals.delete_many({"account_id": acc_id})


# ─────────────────────────── HTTP-layer BOLA ─────────────────────────────

def test_a_attacker_gets_404_on_victim_account(user_a, b_account_id):
    r = user_a.get(f"{API}/brain/costs",
                   params={"symbol": "XAUUSD",
                           "account_id": b_account_id["account_id"]},
                   timeout=30)
    assert r.status_code == 404, \
        f"BOLA leak: got {r.status_code} body={r.text[:400]}"
    body = r.text.lower()
    assert "cost_r" not in body, \
        f"attacker received cost payload: {r.text[:400]}"
    assert "commission" not in body, \
        f"attacker received commission info: {r.text[:400]}"


def test_b_owner_gets_200(user_b, b_account_id):
    r = user_b.get(f"{API}/brain/costs",
                   params={"symbol": "XAUUSD",
                           "account_id": b_account_id["account_id"]},
                   timeout=30)
    assert r.status_code == 200, f"owner blocked: {r.status_code} {r.text}"
    body = r.json()
    assert "cost_r" in body
    assert body["basis"]["account_id"] == b_account_id["account_id"]


def test_c_admin_bypass_ok(admin, b_account_id):
    r = admin.get(f"{API}/brain/costs",
                  params={"symbol": "XAUUSD",
                          "account_id": b_account_id["account_id"]},
                  timeout=30)
    assert r.status_code == 200, r.text
    assert r.json()["basis"]["account_id"] == b_account_id["account_id"]


def test_d_attacker_random_objectid_404(user_a):
    r = user_a.get(f"{API}/brain/costs",
                   params={"symbol": "XAUUSD",
                           "account_id": str(ObjectId())}, timeout=30)
    assert r.status_code == 404, f"expected 404, got {r.status_code}"


def test_e_regression_no_account_id_200(user_a):
    r = user_a.get(f"{API}/brain/costs",
                   params={"symbol": "XAUUSD"}, timeout=30)
    assert r.status_code == 200, r.text
    assert "cost_r" in r.json()


# ─────────────── Defence-in-depth: transaction_costs scoping ─────────────

def test_realized_deals_scoped_to_user_id(b_account_id):
    async def _t():
        db = get_db()
        from transaction_costs import expected_cost_r
        sig = {"entry_price": 2000.0, "stop_loss": 1990.0}
        a_uid = b_account_id["a_uid"]
        b_uid = b_account_id["b_uid"]
        acc = b_account_id["account_id"]

        # attacker uid – broker_deals are B's; user_id filter must
        # exclude them → commission_source NOT realized_deals from B.
        out_a = await expected_cost_r(db, a_uid, "XAUUSD", signal=sig,
                                      account_id=acc)
        assert out_a["basis"]["commission_source"] != "realized_deals" \
            or out_a["basis"]["commission_samples"] == 0, \
            f"attacker leaked realized deals: {out_a['basis']}"

        # owner sees realized_deals
        out_b = await expected_cost_r(db, b_uid, "XAUUSD", signal=sig,
                                      account_id=acc)
        assert out_b["basis"]["commission_source"] == "realized_deals", \
            out_b["basis"]
        assert out_b["basis"]["commission_samples"] > 0
    _run(_t())


# ─────────────────────────── decision_events cap ─────────────────────────

def test_decision_events_capped_at_max_stages():
    async def _t():
        db = get_db()
        from decision_context import MAX_STAGES, mint, record_stage
        sig = {"symbol": "XAUUSD", "action": "buy", "entry_price": 2000.0,
               "stop_loss": 1990.0, "scope": TAG}
        dec_id = await mint(db, f"{TAG}_uid", sig,
                            cfg={"account_id": f"{TAG}_acc"})
        assert dec_id
        try:
            for i in range(45):
                # iter-211: stages must be CANONICAL to be recorded
                await record_stage(db, dec_id, "outcome", {"i": i})
            n = await db.decision_events.count_documents(
                {"decision_id": dec_id})
            assert n == MAX_STAGES == 40, \
                f"expected {MAX_STAGES}, got {n}"
        finally:
            await db.decision_events.delete_many({"decision_id": dec_id})
            await db.decision_contexts.delete_many({"decision_id": dec_id})
    _run(_t())


# ─────────────────────────── module cleanup ──────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _cleanup_users():
    yield
    try:
        m = mongo_db()
        m.users.delete_many({"email": {"$in": [USER_A_EMAIL.lower(),
                                               USER_B_EMAIL.lower()]}})
    except Exception:
        pass


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.integration
