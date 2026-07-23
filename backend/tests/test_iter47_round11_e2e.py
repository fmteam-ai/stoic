"""iter-47 · Round 11 end-to-end verification.

Adds the E2E cases requested in the Round 11 review:
  1. Expired-lease rejection at poll-trades: owner doc present with
     lease_epoch=5 but lease_until in the PAST + pending scalp trade with
     scalp_lease_epoch=5 → CANCEL with error='stale_scalp_lease_epoch'.
  2. Missing owner doc rejection: pending scalp trade with any scalp_lease_epoch
     and NO scalp_owners doc → CANCEL with error='stale_scalp_lease_epoch'.
  3. occurred_at end-to-end (external-deal backfill=true):
       - broker_deals.occurred_at ≈ broker-time-normalized past date (not now).
       - scalp_financial_events.at == occurred_at (== deal_iso) while
         received_at is ~now.
  4. modification_ack success flow: EMERGENCY_STOP_PENDING + MODIFY_SL success
       → stop_loss set, confirmed_stop_loss set, protection_state='RESOLVED',
         protection_missing=False.
  5. modification_ack failure flow: EMERGENCY_STOP_PENDING failure ack →
     protection_state='PROTECTION_UNKNOWN' + last_modification_error recorded.
"""
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
    email = f"TEST_iter47_{uuid.uuid4().hex[:8]}@example.com"
    r = s.post(f"{API}/auth/register",
               json={"terms_agreed": True, "email": email,
                     "password": "Vx7#Qm2pL9wTzK4e", "name": "iter47"},
               timeout=30)
    assert r.status_code == 200, r.text
    mark_email_verified(email)
    r = s.post(f"{API}/auth/login",
               json={"email": email, "password": "Vx7#Qm2pL9wTzK4e"}, timeout=30)
    assert r.status_code == 200, r.text

    r = s.post(f"{API}/accounts", json={
        "label": "TEST_iter47_acc", "broker": "TestBroker", "server": "T",
        "account_number": uuid.uuid4().hex[:8], "account_type": "standard",
        "base_currency": "USD"}, timeout=15)
    assert r.status_code in (200, 201), r.text
    acc = r.json()

    r = s.post(f"{API}/scalp/config", json={
        "account_id": acc["id"], "symbol": "EURUSD",
        "enabled": True, "mode": "shadow"}, timeout=15)
    assert r.status_code == 200, r.text

    yield {"s": s, "email": email, "acc": acc}

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


def _iso(delta_sec: int = 0) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=delta_sec)).isoformat()


def _oid(v):
    from bson import ObjectId
    return ObjectId(v)


def _insert_pending_scalp_trade(db, account_id, user_id, *, epoch, symbol="EURUSD"):
    doc = {
        "account_id": account_id, "user_id": user_id,
        "status": "pending", "scope": "scalp_fast",
        "symbol": symbol, "action": "BUY", "lot_size": 0.01,
        "entry_price": 1.08, "stop_loss": 1.077, "take_profit": 1.085,
        "scalp_lease_epoch": int(epoch),
        "opened_at": _iso(),
    }
    res = db.trades.insert_one(doc)
    return str(res.inserted_id)


# ================================================================
# 1) EXPIRED-LEASE rejection — owner doc present, lease_until in the past
# ================================================================

def test_pollTrades_expired_lease_rejected(ctx):
    db = mongo_db()
    acc = ctx["acc"]
    account_id = acc["id"]
    user_id = str(db.users.find_one({"email": ctx["email"].lower()})["_id"])

    # Install owner doc with correct epoch but EXPIRED lease_until.
    db.scalp_owners.replace_one(
        {"account_id": account_id},
        {"account_id": account_id, "worker_id": "someone:1",
         "lease_until": _iso(-60),          # 60s in the past → expired
         "lease_epoch": 5, "max_order_epoch": 5},
        upsert=True)

    tid = _insert_pending_scalp_trade(db, account_id, user_id, epoch=5)
    r = requests.post(f"{API}/bridge/poll-trades",
                      json={"bridge_token": acc["bridge_token"]}, timeout=15)
    assert r.status_code == 200, r.text
    trade_ids = [t["trade_id"] for t in r.json().get("trades", [])]
    assert tid not in trade_ids, \
        f"expired-lease trade leaked to EA: {trade_ids}"

    doc = db.trades.find_one({"_id": _oid(tid)})
    assert doc.get("status") == "cancelled", f"trade not cancelled: {doc}"
    assert doc.get("error") == "stale_scalp_lease_epoch", \
        f"expected stale_scalp_lease_epoch, got: {doc.get('error')}"
    assert doc.get("close_reason") == "stale_scalp_lease_epoch"

    db.trades.delete_one({"_id": _oid(tid)})
    db.scalp_owners.delete_one({"account_id": account_id})


# ================================================================
# 2) MISSING owner doc — trade with scalp_lease_epoch set → cancelled
# ================================================================

def test_pollTrades_missing_owner_rejected(ctx):
    db = mongo_db()
    acc = ctx["acc"]
    account_id = acc["id"]
    user_id = str(db.users.find_one({"email": ctx["email"].lower()})["_id"])

    # No owner doc at all — every scalp trade must be rejected because no
    # live lease can be proven.
    db.scalp_owners.delete_many({"account_id": account_id})

    tid = _insert_pending_scalp_trade(db, account_id, user_id, epoch=7)
    r = requests.post(f"{API}/bridge/poll-trades",
                      json={"bridge_token": acc["bridge_token"]}, timeout=15)
    assert r.status_code == 200, r.text
    trade_ids = [t["trade_id"] for t in r.json().get("trades", [])]
    assert tid not in trade_ids, \
        f"missing-owner scalp trade leaked to EA: {trade_ids}"

    doc = db.trades.find_one({"_id": _oid(tid)})
    assert doc.get("status") == "cancelled", f"trade not cancelled: {doc}"
    assert doc.get("error") == "stale_scalp_lease_epoch"

    db.trades.delete_one({"_id": _oid(tid)})


# ================================================================
# 3) occurred_at end-to-end — external-deal backfill=true past deal_time
# ================================================================

def test_external_deal_occurred_at_preserves_broker_event_time(ctx):
    db = mongo_db()
    acc = ctx["acc"]
    account_id = acc["id"]
    user_id = str(db.users.find_one({"email": ctx["email"].lower()})["_id"])

    # Ensure THIS server can acquire the lease so apply_broker_deal runs.
    db.scalp_owners.delete_many({"account_id": account_id})

    # Open scalp trade with a specific mt5_ticket
    mt5_ticket = int(time.time() * 1000) % 100000000
    trade_doc = {
        "account_id": account_id, "user_id": user_id,
        "status": "open", "scope": "scalp_fast",
        "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
        "entry_price": 1.08, "stop_loss": 1.077, "take_profit": 1.085,
        "mt5_ticket": mt5_ticket, "opened_at": _iso(),
    }
    db.trades.insert_one(trade_doc)

    # Deal 2 days in the past + backfill=true → occurred_at must equal the
    # normalized past time (offset=0 for a TestBroker so it's the raw epoch
    # rendered as UTC), NOT "now".
    past_epoch = int(time.time()) - 2 * 24 * 3600
    past_iso_expected = datetime.fromtimestamp(
        past_epoch, tz=timezone.utc).isoformat()

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
        "deal_time": past_epoch,
        "magic": 0,
        "position_volume": 0.0,
        "backfill": True,
    }
    r = requests.post(f"{API}/bridge/external-deal",
                      json=payload, timeout=20)
    assert r.status_code == 200, r.text

    time.sleep(1.2)

    # broker_deals must carry occurred_at ≈ past_iso_expected (allow small
    # microsecond precision drift; compare to seconds).
    bd = db.broker_deals.find_one(
        {"deal_id": deal_id, "account_id": account_id})
    assert bd is not None, "broker_deals row missing"
    occurred_at = bd.get("occurred_at")
    assert occurred_at is not None, f"broker_deals missing occurred_at: {bd}"
    occ_dt = datetime.fromisoformat(occurred_at)
    past_dt = datetime.fromtimestamp(past_epoch, tz=timezone.utc)
    drift = abs((occ_dt - past_dt).total_seconds())
    assert drift < 5, \
        f"occurred_at drifted from past deal_time by {drift}s: got {occurred_at}, expected≈{past_iso_expected}"
    # And NOT "now"
    now_drift = abs((occ_dt - datetime.now(timezone.utc)).total_seconds())
    assert now_drift > 3600, \
        f"occurred_at wrongly stamped to ~now: {occurred_at}"

    # scalp_financial_events must have at/occurred_at == occurred_at, but
    # received_at ~ now.
    events = list(db.scalp_financial_events.find(
        {"account_id": account_id, "deal_id": str(deal_id),
         "event_type": "full_close"}))
    assert len(events) == 1, \
        f"expected exactly 1 ledger event, got {len(events)}"
    ev = events[0]
    ev_at = ev.get("at") or ev.get("occurred_at")
    assert ev_at is not None, f"ledger event missing at/occurred_at: {ev}"
    ev_dt = datetime.fromisoformat(ev_at)
    at_drift = abs((ev_dt - past_dt).total_seconds())
    assert at_drift < 5, \
        f"ledger.at drifted from broker occurred_at by {at_drift}s: {ev_at}"

    received_at = ev.get("received_at")
    assert received_at is not None, f"ledger event missing received_at: {ev}"
    r_dt = datetime.fromisoformat(received_at)
    r_now_drift = abs((r_dt - datetime.now(timezone.utc)).total_seconds())
    assert r_now_drift < 300, \
        f"received_at should be ~now, got: {received_at} ({r_now_drift}s off)"
    # Sanity: received_at is meaningfully AFTER the economic event
    assert (r_dt - ev_dt).total_seconds() > 3600, \
        "received_at should be much later than occurred_at for a 2-day-old backfill"


# ================================================================
# 4) modification_ack SUCCESS — EMERGENCY_STOP_PENDING + MODIFY_SL
# ================================================================

def test_modification_ack_success_resolves_emergency(ctx):
    db = mongo_db()
    acc = ctx["acc"]
    account_id = acc["id"]
    user_id = str(db.users.find_one({"email": ctx["email"].lower()})["_id"])

    mt5_ticket = int(time.time() * 1000) % 90000000
    new_sl = 1.077
    doc = {
        "account_id": account_id, "user_id": user_id,
        "status": "open", "scope": "regular",
        "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
        "entry_price": 1.08, "stop_loss": None, "take_profit": 1.085,
        "mt5_ticket": mt5_ticket, "opened_at": _iso(),
        "protection_state": "EMERGENCY_STOP_PENDING",
        "protection_missing": True,
        "pending_modification": {"type": "MODIFY_SL", "new_sl": new_sl},
    }
    tid = str(db.trades.insert_one(doc).inserted_id)

    r = requests.post(f"{API}/bridge/modification-ack", json={
        "bridge_token": acc["bridge_token"], "trade_id": tid,
        "type": "MODIFY_SL", "success": True, "new_sl": new_sl,
    }, timeout=15)
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True

    fresh = db.trades.find_one({"_id": _oid(tid)})
    assert fresh is not None
    assert abs(float(fresh.get("stop_loss") or 0) - new_sl) < 1e-9, \
        f"stop_loss not applied: {fresh.get('stop_loss')}"
    assert abs(float(fresh.get("confirmed_stop_loss") or 0) - new_sl) < 1e-9, \
        f"confirmed_stop_loss not set: {fresh.get('confirmed_stop_loss')}"
    assert fresh.get("protection_state") == "RESOLVED", \
        f"protection_state should be RESOLVED: {fresh.get('protection_state')}"
    assert fresh.get("protection_missing") is False, \
        f"protection_missing should be False: {fresh.get('protection_missing')}"
    assert fresh.get("pending_modification") in (None,), \
        f"pending_modification should be cleared: {fresh.get('pending_modification')}"

    db.trades.delete_one({"_id": _oid(tid)})


# ================================================================
# 5) modification_ack FAILURE — EMERGENCY_STOP_PENDING failure ack
# ================================================================

def test_modification_ack_failure_sets_protection_unknown(ctx):
    db = mongo_db()
    acc = ctx["acc"]
    account_id = acc["id"]
    user_id = str(db.users.find_one({"email": ctx["email"].lower()})["_id"])

    mt5_ticket = int(time.time() * 1000) % 90000000 + 1
    doc = {
        "account_id": account_id, "user_id": user_id,
        "status": "open", "scope": "regular",
        "symbol": "EURUSD", "action": "BUY", "lot_size": 0.01,
        "entry_price": 1.08, "stop_loss": None, "take_profit": 1.085,
        "mt5_ticket": mt5_ticket, "opened_at": _iso(),
        "protection_state": "EMERGENCY_STOP_PENDING",
        "protection_missing": True,
        "pending_modification": {"type": "MODIFY_SL", "new_sl": 1.076},
    }
    tid = str(db.trades.insert_one(doc).inserted_id)

    err_msg = "invalid_stops"
    r = requests.post(f"{API}/bridge/modification-ack", json={
        "bridge_token": acc["bridge_token"], "trade_id": tid,
        "type": "MODIFY_SL", "success": False, "error": err_msg,
    }, timeout=15)
    assert r.status_code == 200, r.text

    fresh = db.trades.find_one({"_id": _oid(tid)})
    assert fresh is not None
    assert fresh.get("protection_state") == "PROTECTION_UNKNOWN", \
        f"expected PROTECTION_UNKNOWN, got: {fresh.get('protection_state')}"
    assert fresh.get("last_modification_error") == err_msg, \
        f"expected last_modification_error={err_msg!r}, got: {fresh.get('last_modification_error')}"
    # stop_loss must NOT have been silently applied on failure
    assert fresh.get("stop_loss") in (None,), \
        f"stop_loss should remain None on failure ack: {fresh.get('stop_loss')}"

    db.trades.delete_one({"_id": _oid(tid)})
