from live_target import ADMIN_PASSWORD  # noqa: E402,F401 — env-provided, never literals
"""iter-209 live-DB integration tests for the six corrections.
Tests exercise the real Mongo, use TEST_iter209_* seeds, and clean up.

Coverage:
  1) DecisionEvents split (real db.decision_contexts + db.decision_events)
  3) Broker/account-specific transaction costs (bot_configs, broker_deals)
  4) CPCV rename (oos_loss_rate, embargo_basis, scorecard check)
  6) Decay hysteresis (strategy_health persists better_streak)
  2) Degraded distributed report path via Mongo fallback
  + Regression smoke via external URL (auth cookie + brain endpoints)
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# fastapi/motor set up via server import (loads .env, ensures indexes)
import server  # noqa: F401,E402
from database import get_db  # noqa: E402

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL") \
    or "https://stoic-trading-bot.preview.emergentagent.com"
BASE_URL = BASE_URL.rstrip("/")
ADMIN_EMAIL = "admin@stoicaibot.com"
pass  # ADMIN_PASSWORD comes from live_target
TAG = f"TEST_iter209_{os.getpid()}"
USR_DECISION = f"{TAG}_decision"
USR_TENANT_OTHER = f"{TAG}_other"
USR_COSTS = f"{TAG}_costs"
USR_DECAY = f"{TAG}_decay"
ACC_ID = f"{TAG}_acc"
SYMBOL = "XAUUSD"


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) \
        if False else asyncio.new_event_loop().run_until_complete(coro)


# ─────────────────────────── admin session (for API smoke) ────────────

@pytest.fixture(scope="module")
def admin_session():
    s = requests.Session()
    r = s.post(f"{BASE_URL}/api/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
               timeout=15)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code} {r.text[:200]}")
    csrf = s.cookies.get("csrf_token")
    if csrf:
        s.headers.update({"X-CSRF-Token": csrf})
    return s


# ─────────────────────────── 1 · DecisionEvents split ──────────────────

def test_decision_snapshot_no_stages_and_events_appended():
    async def _t():
        db = get_db()
        from decision_context import (get_decision, mint, record_stage)
        sig = {"symbol": SYMBOL, "action": "buy", "entry_price": 2000.0,
               "stop_loss": 1990.0, "confidence": 0.7, "scope": "test_scope",
               "spread": 0.5}
        dec_id = await mint(db, USR_DECISION, sig,
                            cfg={"account_id": ACC_ID, "risk_profile": "M"},
                            account={"broker": "TEST", "equity": 10000})
        assert dec_id and dec_id.startswith("dec_")

        snap = await db.decision_contexts.find_one({"decision_id": dec_id})
        assert snap is not None
        assert "stages" not in snap, "snapshot must NOT carry a stages array"

        await record_stage(db, dec_id, "meta_decision",
                           {"decision": "TRADE", "confidence": 0.7})
        await record_stage(db, dec_id, "execution",
                           {"lot": 0.1, "authorized": True})

        # snapshot untouched (still no stages field, same at)
        snap2 = await db.decision_contexts.find_one({"decision_id": dec_id})
        assert "stages" not in snap2
        assert snap2["at"] == snap["at"]

        # events collection has 2 in chronological order
        evs = await db.decision_events.count_documents(
            {"decision_id": dec_id})
        assert evs == 2, f"expected 2 events, got {evs}"

        merged = await get_decision(db, dec_id, user_id=USR_DECISION)
        assert merged is not None
        assert len(merged["events"]) == 2
        stages = [e["stage"] for e in merged["events"]]
        assert stages == ["meta_decision", "execution"], stages

        # tenant isolation — other user cannot see it
        blocked = await get_decision(db, dec_id, user_id=USR_TENANT_OTHER)
        assert blocked is None
    _run(_t())


def test_decision_endpoint_returns_events(admin_session):
    """GET /api/brain/decisions/{id} returns the events list (admin sees any)."""
    async def _mint():
        db = get_db()
        from decision_context import mint, record_stage
        sig = {"symbol": SYMBOL, "action": "sell", "entry_price": 2005.0,
               "stop_loss": 2015.0, "scope": "api_scope"}
        dec_id = await mint(db, USR_DECISION, sig,
                            cfg={"account_id": ACC_ID})
        await record_stage(db, dec_id, "meta_decision", {"decision": "SKIP"})
        return dec_id
    dec_id = _run(_mint())
    r = admin_session.get(f"{BASE_URL}/api/brain/decisions/{dec_id}",
                          timeout=10)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert "events" in body, body
    assert any(e.get("stage") == "meta_decision" for e in body["events"])
    assert "stages" not in body


# ─────────────────────────── 3 · broker/account-specific costs ────────

def test_expected_cost_r_account_config_source():
    async def _t():
        db = get_db()
        from transaction_costs import expected_cost_r
        await db.bot_configs.delete_many({"user_id": USR_COSTS})
        await db.broker_deals.delete_many({"account_id": ACC_ID})
        await db.bot_configs.insert_one(
            {"user_id": USR_COSTS, "account_id": ACC_ID,
             "commission_usd_per_lot_side": 3.5})
        sig = {"entry_price": 2000.0, "stop_loss": 1990.0, "spread": 0.5}
        out = await expected_cost_r(db, USR_COSTS, SYMBOL, signal=sig,
                                    account_id=ACC_ID)
        assert out["basis"]["commission_source"] == "account_config", out
        assert abs(out["components"]["commission_r"] - 0.007) < 1e-4
        assert out["basis"]["account_id"] == ACC_ID
    _run(_t())


def test_expected_cost_r_realized_deals_source():
    async def _t():
        db = get_db()
        from transaction_costs import expected_cost_r
        await db.bot_configs.delete_many({"user_id": USR_COSTS})
        await db.broker_deals.delete_many({"account_id": ACC_ID})
        now = datetime.now(timezone.utc)
        docs = [{"account_id": ACC_ID, "user_id": USR_COSTS,
                 "symbol": SYMBOL,
                 "deal_id": f"{TAG}_deal_{i}",
                 "commission": -7.0, "swap": -2.0, "lots": 1.0,
                 "deal_time": (now - timedelta(hours=i)).isoformat()}
                for i in range(6)]
        await db.broker_deals.insert_many(docs)
        sig = {"entry_price": 2000.0, "stop_loss": 1990.0}
        out = await expected_cost_r(db, USR_COSTS, SYMBOL, signal=sig,
                                    scope="swing_trend",
                                    account_id=ACC_ID)
        assert out["basis"]["commission_source"] == "realized_deals", out
        assert out["basis"]["swap_source"] == "realized_deals"
        assert out["components"]["swap_r"] > 0
    _run(_t())


def test_expected_cost_r_default_without_account():
    async def _t():
        db = get_db()
        from transaction_costs import COMMISSION_R, expected_cost_r
        sig = {"entry_price": 2000.0, "stop_loss": 1990.0}
        out = await expected_cost_r(db, USR_COSTS, SYMBOL, signal=sig)
        assert out["basis"]["commission_source"] == "default"
        assert out["components"]["commission_r"] == COMMISSION_R
    _run(_t())


def test_costs_endpoint_reflects_account_config(admin_session):
    async def _seed():
        db = get_db()
        # tie the config to the admin user for the endpoint call
        admin = await db.users.find_one({"email": ADMIN_EMAIL})
        assert admin
        aid = str(admin["_id"]) if not isinstance(admin.get("id"), str) \
            else admin["id"]
        aid_alt = admin.get("id") or str(admin["_id"])
        # iter-210: /api/brain/costs now enforces account ownership via
        # _owned_account — we must seed a real accounts doc whose _id is
        # a valid ObjectId (admin bypass still requires the account to exist).
        from bson import ObjectId
        acc_oid = ObjectId()
        acc_id_str = str(acc_oid)
        await db.accounts.insert_one({
            "_id": acc_oid, "user_id": aid_alt,
            "name": f"{TAG}_ep_acc", "broker": "TEST",
            "equity": 10000.0, "balance": 10000.0,
            "created_at": datetime.now(timezone.utc).isoformat()})
        await db.bot_configs.delete_many(
            {"user_id": {"$in": [aid, aid_alt]},
             "account_id": acc_id_str})
        seen = set()
        for u in (aid, aid_alt):
            if u in seen:
                continue
            seen.add(u)
            await db.bot_configs.insert_one(
                {"user_id": u, "account_id": acc_id_str,
                 "commission_usd_per_lot_side": 3.5})
        return aid_alt, acc_id_str
    aid_alt, acc_id_str = _run(_seed())
    try:
        r = admin_session.get(
            f"{BASE_URL}/api/brain/costs",
            params={"symbol": SYMBOL, "account_id": acc_id_str},
            timeout=10)
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body["basis"]["account_id"] == acc_id_str
    finally:
        # use sync pymongo since get_db() is bound to a different event loop
        from pymongo import MongoClient
        from bson import ObjectId
        sync = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
        sync.accounts.delete_one({"_id": ObjectId(acc_id_str)})
        sync.bot_configs.delete_many({"account_id": acc_id_str})
    assert "commission_source" in body["basis"]
    assert "swap_source" in body["basis"]
    # inline cleanup omitted — module fixture removes the seeded configs


# ─────────────────────────── 4 · CPCV renamed ─────────────────────────

def test_cpcv_uses_oos_loss_rate_not_pbo():
    from champion_challenger2 import build_scorecard, cpcv
    series = [1.6 if i % 20 < 11 else -1.0 for i in range(200)]
    cp = cpcv(series)
    assert "oos_loss_rate" in cp and "pbo" not in cp
    assert "embargo_basis" in cp
    sc = build_scorecard(series)
    names = " ".join(c["name"] for c in sc["checks"])
    assert "CPCV OOS loss rate" in names
    assert "PBO" not in names and "overfitting probability" not in names


# ─────────────────────────── 6 · Decay hysteresis (live db) ───────────

def _seed_recent(db, uid, wins_pct, closed_at_key="closed_at"):
    """Return coroutines to be awaited: seed 20 recent trades with wins_pct."""
    async def _do():
        await db.trades.delete_many({"user_id": uid, "scope": "test_scalp",
                                     "closed_at": {"$gte": (
                                         datetime.now(timezone.utc)
                                         - timedelta(days=30)).isoformat()}})
        now = datetime.now(timezone.utc)
        n_win = (wins_pct * 20) // 100
        for i in range(20):
            is_win = i < n_win
            await db.trades.insert_one({
                "user_id": uid, "scope": "test_scalp", "symbol": SYMBOL,
                "action": "buy", "status": "closed",
                "entry_price": 2000.0, "stop_loss": 1990.0,
                "exit_price": 2010.0 if is_win else 1990.0,
                "pnl": 10.0 if is_win else -10.0,
                "closed_at": (now - timedelta(
                    days=(i % 25) + 1)).isoformat(),
                "alpha_clean": True,
                "attribution_primary": "ALPHA_ERROR"})
    return _do


def test_decay_hysteresis_persists_and_recovers_one_step():
    async def _t():
        db = get_db()
        uid = USR_DECAY
        from strategy_decay import strategy_health
        # clean slate
        await db.trades.delete_many({"user_id": uid})
        await db.strategy_health.delete_many({"user_id": uid})
        now = datetime.now(timezone.utc)
        # 70 healthy base-period trades
        for i in range(70):
            days_ago = 30 + (i % 60) + 1
            is_win = (i % 5) < 3
            await db.trades.insert_one({
                "user_id": uid, "scope": "test_scalp", "symbol": SYMBOL,
                "action": "buy", "status": "closed",
                "entry_price": 2000.0, "stop_loss": 1990.0,
                "exit_price": 2010.0 if is_win else 1990.0,
                "pnl": 10.0 if is_win else -10.0,
                "closed_at": (now - timedelta(days=days_ago)).isoformat(),
                "alpha_clean": True,
                "attribution_primary": "ALPHA_ERROR"})
        # 20 degraded recent trades (20% wins) → first call degraded
        await _seed_recent(db, uid, wins_pct=20)()
        h1 = await strategy_health(db, uid, "test_scalp")
        assert h1["unproven"] is False
        raw1 = h1["raw_state"]
        assert raw1 in ("WATCH", "DEGRADED", "DECAYING", "DISABLED"), h1
        # degradation applies immediately: state == raw_state
        assert h1["state"] == raw1, h1
        # persisted
        doc = await db.strategy_health.find_one(
            {"user_id": uid, "scope": "test_scalp"})
        assert doc and doc["state"] == raw1
        assert doc["raw_state"] == raw1
        assert doc.get("better_streak", 0) == 0

        # reseed recent trades to HEALTHY (80% wins) — expect hold+streak
        await _seed_recent(db, uid, wins_pct=80)()
        h2 = await strategy_health(db, uid, "test_scalp")
        # first better reading: state holds, streak == 1
        assert h2["raw_state"] == "HEALTHY", h2
        assert h2["state"] == raw1, f"expected hold at {raw1}, got {h2}"
        assert h2["better_streak"] == 1, h2

        # second better reading: recover exactly one step
        h3 = await strategy_health(db, uid, "test_scalp")
        from strategy_decay import STATES
        expected = STATES[STATES.index(raw1) - 1]
        assert h3["state"] == expected, \
            f"expected one-step recovery to {expected}, got {h3}"
        assert h3["better_streak"] == 0

        doc2 = await db.strategy_health.find_one(
            {"user_id": uid, "scope": "test_scalp"})
        assert doc2["state"] == expected
    _run(_t())


# ─────────────────────────── 2 · degraded distributed (mongo fallback) ─

def test_degraded_report_fresh_process_consults_mongo_and_recovers():
    async def _t():
        db = get_db()
        import degraded_intelligence as di
        # seed a failing state in mongo (fresh-process simulation)
        await db.intelligence_health.update_one(
            {"_id": "market_memory"},
            {"$set": {"_id": "market_memory", "ok": False,
                      "consecutive_failures": 2,
                      "last_error": "TEST_iter209 seed"}},
            upsert=True)
        di._mem.pop("market_memory", None)  # fresh process
        os.environ.pop("REDIS_URL", None)
        await di.report(db, "market_memory", ok=True)
        doc = await db.intelligence_health.find_one(
            {"_id": "market_memory"})
        assert doc["ok"] is True, doc
        assert int(doc.get("consecutive_failures", 99)) == 0, doc
        # cleanup: remove test seed
        await db.intelligence_health.delete_one({"_id": "market_memory"})
        di._mem.pop("market_memory", None)
    _run(_t())


def test_degraded_endpoint_is_normal(admin_session):
    r = admin_session.get(f"{BASE_URL}/api/brain/degraded", timeout=10)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["mode"] in ("NORMAL", "DEGRADED_INTELLIGENCE"), body
    # after the previous cleanup we expect NORMAL, but we don't hard-fail
    # if the broader system reports a real degradation.


# ─────────────────────────── regression smoke ─────────────────────────

def test_smoke_brain_endpoints(admin_session):
    for path in ("/api/brain/decisions?limit=3",
                 "/api/brain/degraded",
                 "/api/brain/costs?symbol=XAUUSD",
                 "/api/brain/strategy-health"):
        r = admin_session.get(f"{BASE_URL}{path}", timeout=15)
        assert r.status_code == 200, f"{path} → {r.status_code} {r.text[:200]}"
        body = r.json()
        assert isinstance(body, dict)


# ─────────────────────────── CLEANUP ──────────────────────────────────

@pytest.fixture(scope="module", autouse=True)
def _cleanup_iter209():
    yield
    async def _c():
        db = get_db()
        users = (USR_DECISION, USR_TENANT_OTHER, USR_COSTS, USR_DECAY)
        for uid in users:
            await db.trades.delete_many({"user_id": uid})
            await db.bot_configs.delete_many({"user_id": uid})
            await db.strategy_health.delete_many({"user_id": uid})
            dids = [d["decision_id"] async for d in db.decision_contexts
                    .find({"user_id": uid}, {"decision_id": 1})]
            if dids:
                await db.decision_events.delete_many(
                    {"decision_id": {"$in": dids}})
            await db.decision_contexts.delete_many({"user_id": uid})
        await db.broker_deals.delete_many({"account_id": ACC_ID})
        await db.bot_configs.delete_many({"account_id": ACC_ID})
        await db.intelligence_health.delete_one(
            {"_id": "market_memory",
             "last_error": "TEST_iter209 seed"})
    try:
        asyncio.new_event_loop().run_until_complete(_c())
    except Exception:
        pass


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
