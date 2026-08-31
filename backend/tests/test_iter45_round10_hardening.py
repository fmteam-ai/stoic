"""iter-45 · Round 10 hardening — end-to-end verification.

Covers the two P0 fixes on live HTTP + Mongo:
 1. Poll-trades epoch fencing:
    * a pending scalp trade stamped with an OLDER lease_epoch than the account's
      max_order_epoch gets CANCELLED and NOT returned to the EA.
    * a pending scalp trade stamped with a >= lease_epoch is dispatched, gets
      _dispatched_at stamped, and raises scalp_owners.max_order_epoch.
    * legacy scalp trade (no scalp_lease_epoch) still dispatches normally.
    * non-scalp pending trade is unaffected by epoch fencing.
 2. Ledger-first financial-event state machine:
    * external-deal 'out' → scalp_financial_events doc status='applied',
      risk_applied=True, net_pnl=-1.7. broker_deals status='complete'.
    * crash-recovery via recover_pending_deals reconstructs the ledger event
      when it is missing.
 3. Service-block surfacing on /api/scalp/status admin view.
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

@pytest.fixture(scope="module")
def ctx():
    s = requests.Session()
    email = f"TEST_iter45_{uuid.uuid4().hex[:8]}@example.com"
    r = s.post(f"{API}/auth/register",
               json={"terms_agreed": True, "email": email,
                     "password": "Vx7#Qm2pL9wTzK4e", "name": "iter45"},
               timeout=30)
    assert r.status_code == 200, r.text
    mark_email_verified(email)
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": "Vx7#Qm2pL9wTzK4e"}, timeout=30)
    assert r.status_code == 200, r.text

    r = s.post(f"{API}/accounts", json={
        "label": "TEST_iter45_acc", "broker": "TestBroker", "server": "T",
        "account_number": uuid.uuid4().hex[:8], "account_type": "standard",
        "base_currency": "USD"}, timeout=15)
    assert r.status_code in (200, 201), r.text
    acc = r.json()
    # bridge_token is masked in serialized accounts — fetch a full one
    r = s.post(f"{API}/accounts/{acc['id']}/rotate-token", timeout=15)
    assert r.status_code == 200, r.text
    acc["bridge_token"] = r.json()["bridge_token"]

    # enable scalp config on EURUSD (needed for scalp_fast trades)
    r = s.post(f"{API}/scalp/config", json={
        "account_id": acc["id"], "symbol": "EURUSD",
        "enabled": True, "mode": "shadow"}, timeout=15)
    assert r.status_code == 200, r.text

    yield {"s": s, "email": email, "acc": acc}

    # teardown
    try:
        db = mongo_db()
        db.scalp_configs.delete_many({"account_id": acc["id"]})
        db.scalp_owners.delete_many({"account_id": acc["id"]})
        db.scalp_decisions.delete_many({"account_id": acc["id"]})
        db.scalp_financial_events.delete_many({"account_id": acc["id"]})
        db.broker_deals.delete_many({"account_id": acc["id"]})
        db.trades.delete_many({"account_id": acc["id"]})
        u = db.users.find_one({"email": email.lower()})
        if u:
            uid = str(u["_id"])
            db.intraday_candles.delete_many({"user_id": uid})
            db.accounts.delete_many({"user_id": uid})
            db.users.delete_one({"_id": u["_id"]})
    except Exception as e:  # noqa: BLE001
        print(f"teardown warning: {e}")


def _future_iso(seconds: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _install_alien_owner(db, account_id, *, epoch=5, max_order_epoch=5):
    """Install a scalp_owners doc for a DIFFERENT worker so THIS backend is
    NOT the owner. Used for epoch-fencing tests."""
    db.scalp_owners.replace_one(
        {"account_id": account_id},
        {"account_id": account_id, "worker_id": "other-host:1",
         "lease_until": _future_iso(300),
         "lease_epoch": epoch, "max_order_epoch": max_order_epoch},
        upsert=True)


def _insert_pending_trade(db, account_id, user_id, *,
                           scope="scalp_fast", epoch=None, symbol="EURUSD"):
    doc = {
        "account_id": account_id, "user_id": user_id,
        "status": "pending", "scope": scope,
        "symbol": symbol, "action": "BUY", "lot_size": 0.01,
        "entry_price": 1.08, "stop_loss": 1.077, "take_profit": 1.085,
        "opened_at": _now_iso(),
    }
    if epoch is not None:
        doc["scalp_lease_epoch"] = int(epoch)
    res = db.trades.insert_one(doc)
    return str(res.inserted_id)


# ================================================================
# Test 1 — POLL-TRADES EPOCH FENCING (stale epoch is CANCELLED)
# ================================================================

def test_pollTrades_stale_epoch_cancelled(ctx):
    db = mongo_db()
    acc = ctx["acc"]
    account_id = acc["id"]
    user_id_row = db.users.find_one({"email": ctx["email"].lower()})
    user_id = str(user_id_row["_id"])

    # alien worker owns the account with lease_epoch=5, max_order_epoch=5
    _install_alien_owner(db, account_id, epoch=5, max_order_epoch=5)

    # pending scalp trade stamped with STALE epoch=3
    tid = _insert_pending_trade(db, account_id, user_id, epoch=3)

    r = requests.post(f"{API}/bridge/poll-trades",
                      json={"bridge_token": acc["bridge_token"]}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    trade_ids_returned = [t["trade_id"] for t in body.get("trades", [])]
    assert tid not in trade_ids_returned, \
        f"stale-epoch trade was leaked to EA: {trade_ids_returned}"

    # Trade must be cancelled with error='stale_scalp_lease_epoch'
    doc = db.trades.find_one({"_id": _oid(tid)})
    assert doc is not None
    assert doc.get("status") == "cancelled", f"trade not cancelled: {doc}"
    assert doc.get("error") == "stale_scalp_lease_epoch"
    assert doc.get("close_reason") == "stale_scalp_lease_epoch"

    # cleanup
    db.trades.delete_one({"_id": _oid(tid)})
    db.scalp_owners.delete_one({"account_id": account_id})


# ================================================================
# Test 2 — POLL-TRADES EPOCH PASS-THROUGH
# ================================================================

def test_pollTrades_epoch_geq_dispatches_and_raises_max(ctx):
    """Round 11 — dispatch requires the trade epoch to EQUAL the account's
    CURRENT live lease_epoch (authoritative ownership doc), not merely to
    clear the max_order_epoch watermark."""
    db = mongo_db()
    acc = ctx["acc"]
    account_id = acc["id"]
    user_id_row = db.users.find_one({"email": ctx["email"].lower()})
    user_id = str(user_id_row["_id"])

    _install_alien_owner(db, account_id, epoch=5, max_order_epoch=4)
    tid = _insert_pending_trade(db, account_id, user_id, epoch=5)

    r = requests.post(f"{API}/bridge/poll-trades",
                      json={"bridge_token": acc["bridge_token"]}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    trade_ids = [t["trade_id"] for t in body.get("trades", [])]
    assert tid in trade_ids, \
        f"epoch=5 (== current lease_epoch) trade should be dispatched: {trade_ids}"

    doc = db.trades.find_one({"_id": _oid(tid)})
    assert doc.get("status") == "pending"
    assert doc.get("_dispatched_at") is not None

    owner = db.scalp_owners.find_one({"account_id": account_id})
    assert int(owner.get("max_order_epoch") or 0) == 5, \
        f"max_order_epoch should be raised to 5: {owner}"

    # a NEWER-than-current epoch must ALSO be rejected (epoch mismatch —
    # such an order cannot belong to the current owner)
    tid2 = _insert_pending_trade(db, account_id, user_id, epoch=6)
    r = requests.post(f"{API}/bridge/poll-trades",
                      json={"bridge_token": acc["bridge_token"]}, timeout=15)
    assert r.status_code == 200, r.text
    assert tid2 not in [t["trade_id"] for t in r.json().get("trades", [])]
    doc2 = db.trades.find_one({"_id": _oid(tid2)})
    assert doc2.get("status") == "cancelled"
    assert doc2.get("error") == "stale_scalp_lease_epoch"

    db.trades.delete_many({"_id": {"$in": [_oid(tid), _oid(tid2)]}})
    db.scalp_owners.delete_one({"account_id": account_id})


def test_pollTrades_legacy_scalp_no_epoch_dispatches(ctx):
    """Legacy pending scalp trade WITHOUT scalp_lease_epoch must still be
    dispatched."""
    db = mongo_db()
    acc = ctx["acc"]
    account_id = acc["id"]
    user_id_row = db.users.find_one({"email": ctx["email"].lower()})
    user_id = str(user_id_row["_id"])

    _install_alien_owner(db, account_id, epoch=5, max_order_epoch=5)
    tid = _insert_pending_trade(db, account_id, user_id, epoch=None)  # legacy

    r = requests.post(f"{API}/bridge/poll-trades",
                      json={"bridge_token": acc["bridge_token"]}, timeout=15)
    assert r.status_code == 200, r.text
    trade_ids = [t["trade_id"] for t in r.json().get("trades", [])]
    assert tid in trade_ids, \
        f"legacy scalp trade (no epoch) should dispatch: {trade_ids}"

    db.trades.delete_one({"_id": _oid(tid)})
    db.scalp_owners.delete_one({"account_id": account_id})


def test_pollTrades_non_scalp_unaffected(ctx):
    """A non-scalp pending trade with a stale-looking epoch is unaffected by
    epoch fencing (only scalp_fast scope is gated)."""
    db = mongo_db()
    acc = ctx["acc"]
    account_id = acc["id"]
    user_id_row = db.users.find_one({"email": ctx["email"].lower()})
    user_id = str(user_id_row["_id"])

    _install_alien_owner(db, account_id, epoch=5, max_order_epoch=5)
    tid = _insert_pending_trade(db, account_id, user_id,
                                scope="regular", epoch=None)

    r = requests.post(f"{API}/bridge/poll-trades",
                      json={"bridge_token": acc["bridge_token"]}, timeout=15)
    assert r.status_code == 200, r.text
    trade_ids = [t["trade_id"] for t in r.json().get("trades", [])]
    assert tid in trade_ids, \
        f"non-scalp trade should dispatch normally: {trade_ids}"

    db.trades.delete_one({"_id": _oid(tid)})
    db.scalp_owners.delete_one({"account_id": account_id})


# ================================================================
# Test 3 — LEDGER-FIRST FINANCIAL EVENT (external-deal close)
# ================================================================

def test_external_deal_creates_applied_ledger_event(ctx):
    db = mongo_db()
    acc = ctx["acc"]
    account_id = acc["id"]
    user_id_row = db.users.find_one({"email": ctx["email"].lower()})
    user_id = str(user_id_row["_id"])

    # Ensure THIS worker acquires the lease by deleting any existing owner.
    db.scalp_owners.delete_many({"account_id": account_id})

    # Create an OPEN scalp trade with a specific mt5_ticket
    mt5_ticket = int(time.time() * 1000) % 100000000
    trade_doc = {
        "account_id": account_id, "user_id": user_id,
        "status": "open", "scope": "scalp_fast",
        "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
        "entry_price": 1.08, "stop_loss": 1.077, "take_profit": 1.085,
        "mt5_ticket": mt5_ticket,
        "opened_at": _now_iso(),
    }
    trade_res = db.trades.insert_one(trade_doc)
    trade_id = str(trade_res.inserted_id)

    # POST /bridge/external-deal 'out' close
    deal_id = int(time.time() * 1000)
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
        "magic": 0,
        "position_volume": 0.0,
    }
    r = requests.post(f"{API}/bridge/external-deal", json=payload, timeout=20)
    assert r.status_code == 200, r.text

    # allow async application to settle
    time.sleep(1.0)

    # Verify exactly ONE scalp_financial_events doc for this deal
    events = list(db.scalp_financial_events.find({
        "account_id": account_id, "deal_id": str(deal_id),
        "event_type": "full_close"}))
    assert len(events) == 1, f"expected 1 ledger event, got {len(events)}: {events}"
    ev = events[0]
    assert ev.get("status") == "applied", f"event not applied: {ev}"
    assert ev.get("risk_applied") is True, f"risk_applied not True: {ev}"
    assert abs(ev.get("net_pnl", 0) - (-1.7)) < 1e-6, \
        f"net_pnl should be -1.7 (=-1.5+-0.2+0): {ev.get('net_pnl')}"

    # Verify broker_deals reconciliation_status='complete'
    bd = db.broker_deals.find_one(
        {"deal_id": deal_id, "account_id": account_id})
    assert bd is not None
    assert bd.get("financial_reconciliation_status") == "complete", \
        f"broker_deals status: {bd.get('financial_reconciliation_status')}"

    # persist for use by the next test (recovery)
    ctx["_deal_id"] = deal_id
    ctx["_trade_id"] = trade_id
    ctx["_mt5_ticket"] = mt5_ticket


# ================================================================
# Test 4 — CRASH RECOVERY reconstructs missing ledger event
# ================================================================

def test_crash_recovery_reconstructs_missing_ledger_event(ctx):
    """After the previous test: delete the ledger event and put the deal
    back into 'pending' with an aged received_at → recover_pending_deals()
    must RE-CREATE the ledger event and flip the deal back to complete."""
    if "_deal_id" not in ctx:
        pytest.skip("prerequisite test_external_deal_creates_applied_ledger_event skipped/failed")
    db = mongo_db()
    acc = ctx["acc"]
    account_id = acc["id"]
    deal_id = ctx["_deal_id"]

    # Delete the ledger event and reset broker_deals to pending, aged.
    db.scalp_financial_events.delete_many({
        "account_id": account_id, "deal_id": str(deal_id)})
    # Release the lease held by the BACKEND worker (previous test's HTTP
    # flow) — simulates the crashed owner's lease expiring so the recovery
    # process (this pytest worker) can acquire ownership.
    db.scalp_owners.delete_many({"account_id": account_id})
    from scalp import engine as _eng
    _eng._lease_cache.pop(account_id, None)
    aged = (datetime.now(timezone.utc) - timedelta(seconds=180)).isoformat()
    db.broker_deals.update_one(
        {"deal_id": deal_id, "account_id": account_id},
        {"$set": {"financial_reconciliation_status": "pending",
                  "financial_reconciled_at": None,
                  "received_at": aged,
                  "reconciliation_attempts": 0}})

    # Import the async recover and run it via asyncio
    sys.path.insert(0, str(BACKEND))
    from motor.motor_asyncio import AsyncIOMotorClient
    from scalp.engine import recover_pending_deals

    async def _run():
        client = AsyncIOMotorClient(os.environ["MONGO_URL"])
        adb = client[os.environ["DB_NAME"]]
        return await recover_pending_deals(adb, older_than_sec=60)

    res = asyncio.run(_run())
    print(f"recover_pending_deals result: {res}")

    time.sleep(0.5)

    # Ledger event must have been re-created (idempotent replay is OK; status
    # 'applied' expected because ledger-first upsert is followed by the
    # applied flip; even if risk apply is dedup-skipped, the ledger doc must
    # exist per Round 10 fix.
    events = list(db.scalp_financial_events.find({
        "account_id": account_id, "deal_id": str(deal_id),
        "event_type": "full_close"}))
    assert len(events) == 1, \
        f"ledger event was NOT reconstructed: found {len(events)}"
    ev = events[0]
    # Doc must exist at minimum with pending or applied status.
    assert ev.get("status") in ("applied", "pending"), \
        f"unexpected ledger status: {ev.get('status')}"
    # per spec — Round 10 fix — status should be 'applied' after recovery
    assert ev.get("status") == "applied", \
        f"expected applied after recovery, got: {ev.get('status')}"

    bd = db.broker_deals.find_one(
        {"deal_id": deal_id, "account_id": account_id})
    assert bd.get("financial_reconciliation_status") == "complete", \
        f"deal did not flip back to complete: {bd.get('financial_reconciliation_status')}"


# ================================================================
# Test 5 — service-block surfacing on /api/scalp/status
# ================================================================

def test_scalp_status_admin_shows_service_block_null():
    s = requests.Session()
    r = s.post(f"{API}/auth/login",
               json={"email": "admin@trading.bot", "password": "admin123"},
               timeout=30)
    if r.status_code != 200:
        pytest.skip(f"admin login failed: {r.status_code}")
    r = s.get(f"{API}/scalp/status", timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    audit = body.get("audit") or {}
    assert "service_block" in audit, \
        f"audit missing service_block key: {audit.keys()}"
    assert audit["service_block"] is None, \
        f"service_block should be None on healthy startup: {audit['service_block']}"


# ================================================================
# Test 6 — regression: suffixed-symbol tick ingestion still works
# ================================================================

def test_suffixed_symbol_tick_regression(ctx):
    acc = ctx["acc"]
    # release any lease grabbed in-process by the crash-recovery test (that
    # lease belongs to the PYTEST worker, not the backend server worker)
    mongo_db().scalp_owners.delete_many({"account_id": acc["id"]})
    now = int(time.time() * 1000)
    ticks = [{"tm": now - i * 100, "b": 1.08497 + i * 0.00001,
              "a": 1.08503 + i * 0.00001} for i in range(10)]
    r = requests.post(f"{API}/bridge/ticks", json={
        "bridge_token": acc["bridge_token"], "symbol": "EURUSD#",
        "sent_at_ms": now, "ticks": ticks}, timeout=15)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("status") == "ok", f"suffixed symbol rejected: {body}"


# ---------------- helpers ----------------

def _oid(hex_or_str):
    from bson import ObjectId
    return ObjectId(hex_or_str)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
