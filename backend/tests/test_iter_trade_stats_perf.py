from live_target import ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""Iter-223 · Trades stats perf/correctness regression.

Reproduces the production incident where GET /api/trades/stats fetched full
trade documents and hit the edge timeout. The fix projects only 5 fields.

Two suites:
  * TestTradeStatsCorrectness — admin smoke: /stats, /stats?account_id,
    /trades, /history, /reconcile still work.
  * TestTradeStatsScale — seeds 50k fat closed trades onto a *fresh* user
    and asserts /stats returns in <10s with reconciled totals. Deletes the
    seeded trades and the throwaway user afterwards.
"""
import os
import time
import uuid
from datetime import datetime, timezone

import pytest
import requests
from dotenv import load_dotenv
from pymongo import MongoClient

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL") or "https://stoic-trading-bot.preview.emergentagent.com"
BASE_URL = BASE_URL.rstrip("/")
API = f"{BASE_URL}/api"

ADMIN_EMAIL = "admin@stoicaibot.com"
pass  # ADMIN_PASSWORD comes from live_target
def _mongo():
    return MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]


def _csrf_headers(session: requests.Session) -> dict:
    tok = session.cookies.get("csrf_token") or session.cookies.get("csrf")
    return {"X-CSRF-Token": tok} if tok else {}


@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=30)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    return s


# ---------- CORRECTNESS ----------
class TestTradeStatsCorrectness:

    def test_stats_no_account(self, admin_session):
        r = admin_session.get(f"{API}/trades/stats", timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        for k in ("total_trades", "win_rate", "total_pnl", "avg_win",
                  "avg_loss", "wins", "losses", "bot", "manual",
                  "open_trades", "reconciliation"):
            assert k in d, f"missing key: {k}"
        for sub in ("bot", "manual"):
            for k in ("total_trades", "wins", "losses", "win_rate", "total_pnl"):
                assert k in d[sub], f"{sub}.{k} missing"
        rec = d["reconciliation"]
        for k in ("status", "total_pnl", "sum_components", "components", "delta"):
            assert k in rec, f"reconciliation.{k} missing"
        # bot + manual totals must equal top-level total
        assert d["bot"]["total_trades"] + d["manual"]["total_trades"] == d["total_trades"]

    def test_stats_with_account_filter(self, admin_session):
        # find one admin account id
        r = admin_session.get(f"{API}/accounts", timeout=30)
        assert r.status_code == 200, r.text
        accts = r.json()
        if not accts:
            pytest.skip("admin has no accounts")
        aid = accts[0].get("id") or accts[0].get("_id")
        r = admin_session.get(f"{API}/trades/stats", params={"account_id": aid}, timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "total_trades" in d and "reconciliation" in d

    def test_list_trades(self, admin_session):
        r = admin_session.get(f"{API}/trades", params={"limit": 5}, timeout=30)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_history_range(self, admin_session):
        r = admin_session.get(f"{API}/trades/history",
                              params={"date_from": "2026-08-01",
                                      "date_to": "2026-08-31"}, timeout=30)
        assert r.status_code == 200, r.text
        d = r.json()
        assert "summary" in d and "trades" in d

    def test_reconcile_endpoint(self, admin_session):
        r = admin_session.post(f"{API}/trades/reconcile",
                               headers=_csrf_headers(admin_session), timeout=30)
        # 200 OK expected; some setups return 200 with per-account report
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"


# ---------- SCALE / PERF ----------
class TestTradeStatsScale:
    """Seed 50k fat closed trades on a throwaway user and assert perf."""

    N_TRADES = 50000

    @pytest.fixture(scope="class")
    def fresh_user(self):
        email = f"perf-{uuid.uuid4().hex[:10]}@example.com"
        password = "Kd5#Zt9mW2xVpR7c"
        s = requests.Session()
        r = s.post(f"{API}/auth/register",
                   json={"email": email, "password": password,
                         "name": "Perf QA", "terms_agreed": True}, timeout=30)
        assert r.status_code == 200, f"register: {r.status_code} {r.text[:300]}"
        # mark verified
        db = _mongo()
        db.users.update_one(
            {"email": email.lower()},
            {"$set": {"email_verified": True},
             "$unset": {"activation_token": "", "activation_expires_at": ""}})
        r = s.post(f"{API}/auth/login",
                   json={"email": email, "password": password}, timeout=30)
        assert r.status_code == 200, f"login: {r.status_code} {r.text[:300]}"
        u = db.users.find_one({"email": email.lower()})
        uid = str(u["_id"])
        yield {"session": s, "user_id": uid, "email": email}
        # cleanup
        db.trades.delete_many({"user_id": uid, "seed_tag": "perf-iter223"})
        db.users.delete_one({"_id": u["_id"]})

    def test_seed_and_stats_perf(self, fresh_user):
        db = _mongo()
        uid = fresh_user["user_id"]
        # ensure indexes exist (seed.py runs on backend boot but be safe)
        db.trades.create_index([("user_id", 1), ("status", 1)])
        db.trades.create_index([("user_id", 1), ("closed_at", -1)])

        accounts = [f"acct-{i}-{uuid.uuid4().hex[:6]}" for i in range(4)]
        fat_a = "A" * 1500
        fat_b = "B" * 1500
        now_iso = datetime.now(timezone.utc).isoformat()

        batch = []
        BATCH_SIZE = 2000
        for i in range(self.N_TRADES):
            pnl = 12.5 if i % 3 == 0 else (-7.25 if i % 3 == 1 else 0.0)
            origin = "auto" if i % 2 == 0 else "manual"
            doc = {
                "user_id": uid,
                "account_id": accounts[i % 4],
                "status": "closed",
                "pnl": pnl,
                "origin": origin,
                "signal_id": (f"sig-{i}" if origin == "auto" else None),
                "magic_number": 901234,
                "symbol": "EURUSD",
                "action": "BUY" if i % 2 == 0 else "SELL",
                "lot_size": 0.10,
                "entry_price": 1.1000,
                "exit_price": 1.1010,
                "opened_at": now_iso,
                "closed_at": now_iso,
                "seed_tag": "perf-iter223",
                "audit_blob_a": fat_a,
                "audit_blob_b": fat_b,
            }
            batch.append(doc)
            if len(batch) >= BATCH_SIZE:
                db.trades.insert_many(batch, ordered=False)
                batch = []
        if batch:
            db.trades.insert_many(batch, ordered=False)

        # Now call /stats and time it
        s = fresh_user["session"]
        t0 = time.perf_counter()
        r = s.get(f"{API}/trades/stats", timeout=30)
        elapsed = time.perf_counter() - t0
        print(f"\n[perf] /api/trades/stats over {self.N_TRADES} fat trades: {elapsed:.2f}s")
        assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
        assert elapsed < 10.0, f"stats too slow: {elapsed:.2f}s"
        d = r.json()
        assert d["total_trades"] == self.N_TRADES, f"got {d['total_trades']}"
        assert d["bot"]["total_trades"] + d["manual"]["total_trades"] == self.N_TRADES
        assert d["reconciliation"]["status"] == "RECONCILED", d["reconciliation"]
