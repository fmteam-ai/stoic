"""iter-48 · Round 12 end-to-end verification.

Covers the Round 12 items:
  1. Ownership intent split (recovery takeover):
     - RECOVERY sweep: expired FOREIGN owner doc + open scalp trade + pending
       broker_deal → recover_pending_deals FLIPS the deal to 'complete' AND
       the scalp_owners doc is taken over (worker_id == recovery worker,
       lease_epoch incremented).
     - LIVE path: expired FOREIGN owner doc + open scalp trade + external-deal
       POST → deal STAYS pending with reconciliation_error =
       'account_owned_by_other_worker' (live path refuses takeover).
       A subsequent recovery sweep then completes it.
  2. Explicit state transitions on scalp_financial_events:
     - After a self-owned (no scalp_owners doc → first-touch adopt) live
       external-deal, the ledger event has status='applied', risk_applied=True,
       apply_attempts >= 1, last_attempt_at set, and reconcile_delay_sec
       AND report_delay_sec are numeric.
  3. Invariant scan telemetry surface:
     - GET /api/scalp/status returns audit.invariant_scan with the expected
       keys, AND a direct in-process call to verify_durable_invariants(db)
       populates the last_success_at + docs_examined counters without raising.
  4. Protection resolution policy (confirmed-only):
     - Open trade with protection_missing=True + stop_loss set + NO
       confirmed_stop_loss → repair_unprotected_positions moves it to
       EMERGENCY_STOP_PENDING with MODIFY_SL (NOT resolved).
     - Then set confirmed_stop_loss + clear pending_modification → next
       repair sweep resolves it (protection_missing=False,
       protection_state='RESOLVED').
"""
import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tests"))

from helpers import base_url, mark_email_verified, mongo_db  # noqa: E402

API = f"{base_url()}/api"


# ---------------- fixtures ----------------

def _iso(delta_sec: int = 0) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=delta_sec)).isoformat()


def _oid(v):
    from bson import ObjectId
    return ObjectId(v)


def _make_ctx(tag: str):
    """Fresh isolated user + TestBroker account + scalp config."""
    s = requests.Session()
    email = f"TEST_iter48_{tag}_{uuid.uuid4().hex[:8]}@example.com"
    r = s.post(f"{API}/auth/register",
               json={"terms_agreed": True, "email": email,
                     "password": "testpass123", "name": f"iter48_{tag}"},
               timeout=30)
    assert r.status_code == 200, r.text
    mark_email_verified(email)
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": "testpass123"}, timeout=30)
    assert r.status_code == 200, r.text

    r = s.post(f"{API}/accounts", json={
        "label": f"TEST_iter48_{tag}",
        "broker": "TestBroker", "server": "T",
        "account_number": uuid.uuid4().hex[:8],
        "account_type": "standard", "base_currency": "USD"}, timeout=15)
    assert r.status_code in (200, 201), r.text
    acc = r.json()

    r = s.post(f"{API}/scalp/config", json={
        "account_id": acc["id"], "symbol": "EURUSD",
        "enabled": True, "mode": "shadow"}, timeout=15)
    assert r.status_code == 200, r.text

    return {"s": s, "email": email, "acc": acc}


def _teardown_ctx(ctx):
    try:
        db = mongo_db()
        acc = ctx["acc"]
        db.scalp_configs.delete_many({"account_id": acc["id"]})
        db.scalp_owners.delete_many({"account_id": acc["id"]})
        db.scalp_risk_state.delete_many({"account_id": acc["id"]})
        db.scalp_decisions.delete_many({"account_id": acc["id"]})
        db.scalp_financial_events.delete_many({"account_id": acc["id"]})
        db.broker_deals.delete_many({"account_id": acc["id"]})
        db.trades.delete_many({"account_id": acc["id"]})
        db.notifications.delete_many({
            "user_id": {"$in": [acc.get("user_id"), None]}})
        u = db.users.find_one({"email": ctx["email"].lower()})
        if u:
            uid = str(u["_id"])
            db.intraday_candles.delete_many({"user_id": uid})
            db.accounts.delete_many({"user_id": uid})
            db.notifications.delete_many({"user_id": uid})
            db.subscriptions.delete_many({"user_id": uid})
            db.users.delete_one({"_id": u["_id"]})
    except Exception as e:  # noqa: BLE001
        print(f"teardown warning: {e}")


@pytest.fixture
def ctx_a():
    c = _make_ctx("A")
    yield c
    _teardown_ctx(c)


@pytest.fixture
def ctx_b():
    c = _make_ctx("B")
    yield c
    _teardown_ctx(c)


@pytest.fixture
def ctx_c():
    c = _make_ctx("C")
    yield c
    _teardown_ctx(c)


def _insert_open_scalp_trade(db, account_id, user_id, *, mt5_ticket,
                             symbol="EURUSD", lot=0.01, sl=1.077):
    doc = {
        "account_id": account_id, "user_id": user_id,
        "status": "open", "scope": "scalp_fast",
        "symbol": symbol, "action": "BUY", "lot_size": lot,
        "entry_price": 1.08, "stop_loss": sl, "take_profit": 1.085,
        "mt5_ticket": mt5_ticket, "opened_at": _iso(-30),
    }
    return str(db.trades.insert_one(doc).inserted_id)


def _insert_pending_broker_deal(db, account_id, user_id, *, mt5_ticket,
                                deal_id, received_delta_sec=-180):
    """Insert an already-pending 'out' broker_deal older than 60s so
    recover_pending_deals will pick it up."""
    doc = {
        "deal_id": deal_id, "account_id": account_id, "user_id": user_id,
        "mt5_ticket": mt5_ticket, "deal_entry": "out",
        "symbol": "EURUSD", "action": "SELL", "lots": 0.01,
        "price": 1.079, "profit": -1.5, "commission": -0.2, "swap": 0.0,
        "deal_time": int(time.time()) - 200, "magic": 901234,
        "position_volume": 0.0,
        "financial_reconciliation_status": "pending",
        "financial_reconciled_at": None,
        "received_at": _iso(received_delta_sec),
        "occurred_at": _iso(received_delta_sec),
    }
    db.broker_deals.insert_one(doc)
    return deal_id


def _user_id_for(email):
    db = mongo_db()
    u = db.users.find_one({"email": email.lower()})
    assert u, f"user missing: {email}"
    return str(u["_id"])


# ================================================================
# 1a) RECOVERY takeover of an expired foreign owner doc
# ================================================================

def test_recovery_takeover_completes_pending_deal(ctx_a):
    """Account A: expired foreign owner + pending deal → recover_pending_deals
    takes over ownership AND flips the deal to complete."""
    db = mongo_db()
    acc = ctx_a["acc"]
    account_id = acc["id"]
    user_id = _user_id_for(ctx_a["email"])

    ghost_worker = f"ghost:{uuid.uuid4().hex[:6]}"
    # Expired FOREIGN owner doc
    db.scalp_owners.replace_one(
        {"account_id": account_id},
        {"account_id": account_id,
         "worker_id": ghost_worker,
         "lease_until": _iso(-600),   # expired 10 minutes ago
         "lease_epoch": 3,
         "max_order_epoch": 3},
        upsert=True)

    mt5_ticket = int(time.time() * 1000) % 90000000
    _insert_open_scalp_trade(db, account_id, user_id, mt5_ticket=mt5_ticket)
    deal_id = int(time.time() * 1000)
    _insert_pending_broker_deal(db, account_id, user_id,
                                mt5_ticket=mt5_ticket, deal_id=deal_id)

    # Directly invoke recovery — proves the mechanism (worker_id will be the
    # snippet's worker, not the server's, but that still confirms takeover).
    import scalp.engine as eng

    async def _go():
        return await eng.recover_pending_deals(db=eng_db_async_wrapper(db),
                                               older_than_sec=60, limit=10)

    async def _wrapper():
        # engine expects motor/async DB
        from motor.motor_asyncio import AsyncIOMotorClient
        cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
        adb = cli[os.environ["DB_NAME"]]
        try:
            return await eng.recover_pending_deals(
                adb, older_than_sec=60, limit=10)
        finally:
            cli.close()

    result = asyncio.run(_wrapper())
    print("recovery result:", result)
    # deal must now be complete
    bd = db.broker_deals.find_one(
        {"deal_id": deal_id, "account_id": account_id})
    assert bd is not None
    assert bd.get("financial_reconciliation_status") == "complete", \
        f"expected complete, got: {bd.get('financial_reconciliation_status')} "\
        f"error={bd.get('reconciliation_error')}"
    assert bd.get("financial_reconciled_by") == "recovery_job"

    # scalp_owners doc taken over (worker_id != ghost, lease_epoch incremented)
    owner = db.scalp_owners.find_one({"account_id": account_id})
    assert owner is not None, "owner doc disappeared"
    assert owner.get("worker_id") != ghost_worker, \
        f"ownership not taken over — still {owner.get('worker_id')}"
    assert int(owner.get("lease_epoch") or 0) > 3, \
        f"lease_epoch not incremented on takeover: {owner.get('lease_epoch')}"


# stub helper referenced above only to keep the async wrapper self-contained
def eng_db_async_wrapper(db):
    return db


# ================================================================
# 1b) LIVE path refuses expired foreign owner (external-deal)
# ================================================================

def test_live_path_refuses_expired_foreign_owner(ctx_b):
    """Account B: expired foreign owner + external-deal POST → deal stays
    pending with reconciliation_error='account_owned_by_other_worker'.
    Recovery sweep afterwards completes it."""
    db = mongo_db()
    acc = ctx_b["acc"]
    account_id = acc["id"]
    user_id = _user_id_for(ctx_b["email"])

    ghost_worker = f"ghost:{uuid.uuid4().hex[:6]}"
    db.scalp_owners.replace_one(
        {"account_id": account_id},
        {"account_id": account_id, "worker_id": ghost_worker,
         "lease_until": _iso(-600), "lease_epoch": 3,
         "max_order_epoch": 3},
        upsert=True)

    mt5_ticket = int(time.time() * 1000) % 80000000
    _insert_open_scalp_trade(db, account_id, user_id, mt5_ticket=mt5_ticket)

    deal_id = int(time.time() * 1000) + 7
    payload = {
        "bridge_token": acc["bridge_token"],
        "mt5_ticket": mt5_ticket,
        "deal_id": deal_id,
        "deal_entry": "out",
        "symbol": "EURUSD",
        "action": "SELL",
        "lots": 0.01,
        "price": 1.079,
        "profit": -1.5,
        "commission": -0.2,
        "swap": 0.0,
        "deal_time": int(time.time()),
        "magic": 901234,
        "position_volume": 0.0,
    }
    r = requests.post(f"{API}/bridge/external-deal", json=payload, timeout=20)
    assert r.status_code == 200, r.text
    time.sleep(1.2)

    bd = db.broker_deals.find_one(
        {"deal_id": deal_id, "account_id": account_id})
    assert bd is not None
    # live path must NOT complete: it should be pending with the specific
    # ownership refusal error.
    assert bd.get("financial_reconciliation_status") == "pending", \
        f"live path completed a foreign-owned deal: {bd}"
    assert bd.get("reconciliation_error") == "account_owned_by_other_worker", \
        f"unexpected error: {bd.get('reconciliation_error')}"

    # owner doc UNCHANGED by the live path (must still be ghost)
    owner = db.scalp_owners.find_one({"account_id": account_id})
    assert owner is not None
    assert owner.get("worker_id") == ghost_worker, \
        f"live path took over ownership: {owner.get('worker_id')} vs {ghost_worker}"

    # Now age the received_at so the recovery sweep picks it up (older than 60s)
    db.broker_deals.update_one(
        {"deal_id": deal_id, "account_id": account_id},
        {"$set": {"received_at": _iso(-120)}})

    # Recovery sweep must take over and complete the deal
    import scalp.engine as eng

    async def _sweep():
        from motor.motor_asyncio import AsyncIOMotorClient
        cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
        adb = cli[os.environ["DB_NAME"]]
        try:
            return await eng.recover_pending_deals(
                adb, older_than_sec=60, limit=10)
        finally:
            cli.close()

    _ = asyncio.run(_sweep())

    bd2 = db.broker_deals.find_one(
        {"deal_id": deal_id, "account_id": account_id})
    assert bd2.get("financial_reconciliation_status") == "complete", \
        f"recovery sweep did not complete the deal: {bd2}"


# ================================================================
# 2) State transitions: applied + latency fields on ledger events
# ================================================================

def test_state_transitions_ledger_event_applied(ctx_c):
    """Self-owned account (no scalp_owners doc → first-touch adopt) →
    external-deal → ledger event ends in status='applied' with
    apply_attempts, last_attempt_at, report_delay_sec, reconcile_delay_sec."""
    db = mongo_db()
    acc = ctx_c["acc"]
    account_id = acc["id"]
    user_id = _user_id_for(ctx_c["email"])

    # Ensure first-touch adoption path
    db.scalp_owners.delete_many({"account_id": account_id})

    mt5_ticket = int(time.time() * 1000) % 70000000
    _insert_open_scalp_trade(db, account_id, user_id, mt5_ticket=mt5_ticket)
    deal_id = int(time.time() * 1000) + 11
    payload = {
        "bridge_token": acc["bridge_token"],
        "mt5_ticket": mt5_ticket, "deal_id": deal_id,
        "deal_entry": "out",
        "symbol": "EURUSD", "action": "SELL", "lots": 0.01,
        "price": 1.079, "profit": -1.5,
        "commission": -0.2, "swap": 0.0,
        "deal_time": int(time.time()),
        "magic": 901234, "position_volume": 0.0,
    }
    r = requests.post(f"{API}/bridge/external-deal", json=payload, timeout=20)
    assert r.status_code == 200, r.text
    time.sleep(1.2)

    # broker deal must have flipped to complete on the live path (self-owned)
    bd = db.broker_deals.find_one(
        {"deal_id": deal_id, "account_id": account_id})
    assert bd is not None
    assert bd.get("financial_reconciliation_status") == "complete", \
        f"expected complete on self-owned live path, got: {bd}"

    ev = db.scalp_financial_events.find_one(
        {"account_id": account_id, "deal_id": str(deal_id)})
    assert ev is not None, "ledger event missing"
    assert ev.get("status") == "applied", \
        f"expected status=applied, got: {ev.get('status')}"
    assert ev.get("risk_applied") is True, \
        f"risk_applied not set: {ev.get('risk_applied')}"
    assert int(ev.get("apply_attempts") or 0) >= 1, \
        f"apply_attempts should be >=1, got: {ev.get('apply_attempts')}"
    assert ev.get("last_attempt_at") is not None, \
        "last_attempt_at should be stamped"
    # Latency fields present and numeric
    for k in ("report_delay_sec", "reconcile_delay_sec"):
        assert k in ev, f"latency field missing: {k}"
        assert isinstance(ev[k], (int, float)), \
            f"latency field {k} not numeric: {ev[k]!r}"


# ================================================================
# 3) Invariant scan telemetry
# ================================================================

def test_invariant_scan_status_and_direct_invocation(ctx_c):
    """GET /api/scalp/status → audit.invariant_scan has expected keys.
    AND direct in-process call to verify_durable_invariants populates
    last_success_at + docs_examined without raising."""
    s = ctx_c["s"]
    r = s.get(f"{API}/scalp/status", timeout=15)
    assert r.status_code == 200, r.text
    js = r.json()
    audit = js.get("audit") or {}
    inv = audit.get("invariant_scan")
    assert isinstance(inv, dict), f"audit.invariant_scan missing: {audit}"
    for key in ("last_attempt_at", "last_success_at", "last_error",
                "last_duration_ms", "docs_examined", "blocked_accounts",
                "ledger_mismatches", "oldest_pending_event_sec"):
        assert key in inv, f"invariant_scan missing key '{key}': {inv}"

    # Direct invocation — proves the scan runs cleanly against real data.
    import scalp.engine as eng

    async def _scan():
        from motor.motor_asyncio import AsyncIOMotorClient
        cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
        adb = cli[os.environ["DB_NAME"]]
        try:
            return await eng.verify_durable_invariants(adb)
        finally:
            cli.close()

    out = asyncio.run(_scan())
    assert isinstance(out, dict), f"expected dict, got: {type(out)}"
    for key in ("blocked", "financial_blocked", "financial_warnings"):
        assert key in out, f"scan result missing key '{key}': {out}"
    # After a direct call the counters must be populated
    assert eng._invariant_scan.get("last_success_at") is not None
    assert isinstance(eng._invariant_scan.get("docs_examined"), int)
    assert eng._invariant_scan.get("docs_examined") >= 0


# ================================================================
# 4) Protection resolution policy — confirmed-only evidence
# ================================================================

def test_protection_resolves_only_with_confirmed_stop_loss(ctx_c):
    """Open trade with protection_missing=True + stop_loss=1.079 + NO
    confirmed_stop_loss → repair sweep moves it to EMERGENCY_STOP_PENDING
    with MODIFY_SL (NOT resolved). Then set confirmed_stop_loss + clear
    pending_modification → repair sweep resolves it."""
    db = mongo_db()
    acc = ctx_c["acc"]
    account_id = acc["id"]
    user_id = _user_id_for(ctx_c["email"])

    # Ensure account has meaningful equity so calculate_emergency_stop
    # returns a real SL (not None → EMERGENCY_CLOSE_PENDING).
    db.accounts.update_one(
        {"_id": _oid(account_id)},
        {"$set": {"equity": 10_000.0, "balance": 10_000.0}})

    mt5_ticket = int(time.time() * 1000) % 60000000 + 3
    doc = {
        "account_id": account_id, "user_id": user_id,
        "status": "open", "scope": "regular",
        "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
        "entry_price": 1.08, "stop_loss": 1.079, "take_profit": 1.085,
        "mt5_ticket": mt5_ticket, "opened_at": _iso(-30),
        "protection_missing": True,
        # note: NO confirmed_stop_loss
    }
    tid = str(db.trades.insert_one(doc).inserted_id)

    import protection_guard as pg

    async def _repair():
        from motor.motor_asyncio import AsyncIOMotorClient
        cli = AsyncIOMotorClient(os.environ["MONGO_URL"])
        adb = cli[os.environ["DB_NAME"]]
        try:
            return await pg.repair_unprotected_positions(adb)
        finally:
            cli.close()

    try:
        # Stage 1 — local stop_loss alone must NOT resolve
        out1 = asyncio.run(_repair())
        assert isinstance(out1, dict)
        assert "awaiting" in out1 and "processed" in out1 \
            and "oldest_unresolved_age_sec" in out1, \
            f"repair queue metrics missing: {out1}"

        fresh = db.trades.find_one({"_id": _oid(tid)})
        assert fresh is not None
        assert fresh.get("protection_missing") is True, \
            "protection_missing was cleared without confirmed_stop_loss"
        # protection queued a stop (either EMERGENCY_STOP_PENDING w/ MODIFY_SL
        # or, if attempts already exhausted, EMERGENCY_CLOSE_PENDING); but the
        # single first sweep of a fresh trade with entry_price + lot + equity
        # should choose EMERGENCY_STOP_PENDING w/ MODIFY_SL.
        assert fresh.get("protection_state") == "EMERGENCY_STOP_PENDING", \
            f"expected EMERGENCY_STOP_PENDING, got: {fresh.get('protection_state')}"
        mod = fresh.get("pending_modification") or {}
        assert mod.get("type") == "MODIFY_SL", \
            f"expected pending_modification.type=MODIFY_SL, got: {mod}"

        # Stage 2 — provide broker-confirmed evidence
        db.trades.update_one(
            {"_id": _oid(tid)},
            {"$set": {"confirmed_stop_loss": 1.079},
             "$unset": {"pending_modification": ""}})

        out2 = asyncio.run(_repair())
        assert isinstance(out2, dict)

        after = db.trades.find_one({"_id": _oid(tid)})
        assert after.get("protection_missing") is False, \
            f"protection_missing not cleared after confirmed evidence: {after}"
        assert after.get("protection_state") == "RESOLVED", \
            f"expected RESOLVED, got: {after.get('protection_state')}"
    finally:
        # Always clean up so the server's protection sweep does not
        # keep processing this trade.
        db.trades.delete_one({"_id": _oid(tid)})
