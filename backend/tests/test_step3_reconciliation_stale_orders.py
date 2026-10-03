"""Roadmap step 3 — reconciliation & stale orders (C3, C4).

C3  heartbeat reconciliation needs CONSECUTIVE misses (+ a minimum missing time)
    before it ghost-closes a DB-open trade; a ticket seen again resets the streak;
    operator sources (manual / force) still close immediately.
C4  /bridge/poll-trades EXPIRES pending NEW orders older than PENDING_ORDER_TTL_SECONDS
    instead of dispatching them; close requests and ticketed rows never expire.
"""
import os
import sys
import pytest
from datetime import datetime, timedelta, timezone
from bson import ObjectId
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from conftest import run_async as _arun  # noqa: E402
except ImportError:  # pragma: no cover
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "_tests_root_conftest",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "conftest.py"))
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _arun = _mod.run_async

TAG = "_test_step3"


def _iso(seconds_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


@pytest.fixture
def db():
    client = MongoClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


@pytest.fixture
def seeded(db):
    user_id = "step3-recon-user"
    token = f"STEP3-{_iso()}"
    acc_id = db.accounts.insert_one({
        "user_id": user_id, "label": "Step3", "broker": "test", "bridge_token": token,
        "open_positions": 2, "last_heartbeat": _iso(), TAG: True,
    }).inserted_id
    ids = {}
    for name, tk in (("a", 880001), ("b", 880002)):
        ids[name] = db.trades.insert_one({
            "user_id": user_id, "account_id": str(acc_id), "symbol": "XAUUSD", "action": "BUY",
            "lot_size": 0.01, "entry_price": 4000, "stop_loss": 3990, "take_profit": 4020,
            "status": "open", "mt5_ticket": tk, "opened_at": _iso(120), TAG: True,
        }).inserted_id
    yield {"user_id": user_id, "account_id": str(acc_id), "token": token, "ids": ids}
    db.trades.delete_many({TAG: True})
    db.accounts.delete_many({TAG: True})
    db.execution_intents.delete_many({TAG: True})


# ------------------------------------------------------------------ C3
def test_c3_single_missing_heartbeat_does_not_close(db, seeded, monkeypatch):
    monkeypatch.setenv("RECONCILE_MISS_CONFIRMATIONS", "2")
    monkeypatch.setenv("RECONCILE_MISS_MIN_SECONDS", "0")
    from trade_reconciler import reconcile_account
    s = _arun(reconcile_account(seeded["account_id"], [], source="heartbeat"))
    assert s["closed_count"] == 0 and s["deferred_count"] == 2
    a = db.trades.find_one({"_id": seeded["ids"]["a"]})
    assert a["status"] == "open" and a["reconcile_miss_streak"] == 1
    assert a.get("reconcile_first_missing_at")


def test_c3_second_consecutive_miss_closes(db, seeded, monkeypatch):
    monkeypatch.setenv("RECONCILE_MISS_CONFIRMATIONS", "2")
    monkeypatch.setenv("RECONCILE_MISS_MIN_SECONDS", "0")
    from trade_reconciler import reconcile_account
    _arun(reconcile_account(seeded["account_id"], [], source="heartbeat"))
    s = _arun(reconcile_account(seeded["account_id"], [], source="heartbeat"))
    assert s["closed_count"] == 2
    a = db.trades.find_one({"_id": seeded["ids"]["a"]})
    assert a["status"] == "closed" and a["reconcile_missed_heartbeats"] == 2
    assert "reconcile_miss_streak" not in a


def test_c3_ticket_seen_again_resets_streak(db, seeded, monkeypatch):
    monkeypatch.setenv("RECONCILE_MISS_CONFIRMATIONS", "2")
    monkeypatch.setenv("RECONCILE_MISS_MIN_SECONDS", "0")
    from trade_reconciler import reconcile_account
    _arun(reconcile_account(seeded["account_id"], [], source="heartbeat"))            # glitch: both missing
    _arun(reconcile_account(seeded["account_id"], [880001, 880002], source="heartbeat"))  # both back
    a = db.trades.find_one({"_id": seeded["ids"]["a"]})
    assert a["status"] == "open" and "reconcile_miss_streak" not in a
    s = _arun(reconcile_account(seeded["account_id"], [], source="heartbeat"))        # miss #1 again
    assert s["closed_count"] == 0 and s["deferred_count"] == 2


def test_c3_minimum_missing_time_is_enforced(db, seeded, monkeypatch):
    monkeypatch.setenv("RECONCILE_MISS_CONFIRMATIONS", "2")
    monkeypatch.setenv("RECONCILE_MISS_MIN_SECONDS", "3600")
    from trade_reconciler import reconcile_account
    for _ in range(3):   # a burst of heartbeats within the same second must not close
        s = _arun(reconcile_account(seeded["account_id"], [], source="heartbeat"))
    assert s["closed_count"] == 0 and s["deferred_count"] == 2
    # back-date the first-missing watermark → now eligible
    db.trades.update_many({TAG: True}, {"$set": {"reconcile_first_missing_at": _iso(7200)}})
    s = _arun(reconcile_account(seeded["account_id"], [], source="heartbeat"))
    assert s["closed_count"] == 2


def test_c3_operator_sources_still_close_immediately(db, seeded):
    from trade_reconciler import reconcile_account
    s = _arun(reconcile_account(seeded["account_id"], [], source="manual"))
    assert s["closed_count"] == 2 and s["confirmations_required"] == 1


def test_c3_only_the_missing_ticket_is_tracked(db, seeded, monkeypatch):
    monkeypatch.setenv("RECONCILE_MISS_CONFIRMATIONS", "2")
    monkeypatch.setenv("RECONCILE_MISS_MIN_SECONDS", "0")
    from trade_reconciler import reconcile_account
    _arun(reconcile_account(seeded["account_id"], [880002], source="heartbeat"))
    s = _arun(reconcile_account(seeded["account_id"], [880002], source="heartbeat"))
    assert s["closed_count"] == 1
    assert db.trades.find_one({"_id": seeded["ids"]["a"]})["status"] == "closed"
    assert db.trades.find_one({"_id": seeded["ids"]["b"]})["status"] == "open"


# ------------------------------------------------------------------ C4
def _poll(token):
    from routes.bridge_routes import poll_trades, PollRequest
    return _arun(poll_trades(PollRequest(bridge_token=token)))


def test_c4_hours_old_pending_order_expires_instead_of_dispatching(db, seeded, monkeypatch):
    monkeypatch.setenv("PENDING_ORDER_TTL_SECONDS", "300")
    intent_id = f"step3-{ObjectId()}"
    db.execution_intents.insert_one({"intent_id": intent_id, "status": "dispatched", TAG: True})
    stale_id = db.trades.insert_one({
        "user_id": seeded["user_id"], "account_id": seeded["account_id"], "symbol": "XAUUSD",
        "action": "BUY", "lot_size": 0.01, "entry_price": 4000, "stop_loss": 3990, "take_profit": 4020,
        "status": "pending", "mt5_ticket": None, "opened_at": _iso(3 * 3600),
        "execution_intent_id": intent_id, TAG: True,
    }).inserted_id
    fresh_id = db.trades.insert_one({
        "user_id": seeded["user_id"], "account_id": seeded["account_id"], "symbol": "XAUUSD",
        "action": "SELL", "lot_size": 0.01, "entry_price": 4000, "stop_loss": 4010, "take_profit": 3980,
        "status": "pending", "mt5_ticket": None, "opened_at": _iso(5), TAG: True,
    }).inserted_id
    resp = _poll(seeded["token"])
    dispatched = {t["trade_id"] for t in resp["trades"]}
    assert str(fresh_id) in dispatched
    assert str(stale_id) not in dispatched
    stale = db.trades.find_one({"_id": stale_id})
    assert stale["status"] == "cancelled" and stale["close_reason"] == "expired"
    assert stale["error"] == "pending_order_expired"
    assert stale["expired_after_s"] == 300
    assert db.execution_intents.find_one({"intent_id": intent_id})["status"] == "expired"


def test_c4_close_requests_never_expire(db, seeded, monkeypatch):
    monkeypatch.setenv("PENDING_ORDER_TTL_SECONDS", "300")
    close_id = db.trades.insert_one({
        "user_id": seeded["user_id"], "account_id": seeded["account_id"], "symbol": "XAUUSD",
        "action": "BUY", "lot_size": 0.01, "entry_price": 4000, "stop_loss": 3990, "take_profit": 4020,
        "status": "pending", "close_requested": True, "mt5_ticket": 880009,
        "opened_at": _iso(3 * 3600), TAG: True,
    }).inserted_id
    resp = _poll(seeded["token"])
    assert str(close_id) in {t["trade_id"] for t in resp["trades"]}
    assert db.trades.find_one({"_id": close_id})["status"] == "pending"


def test_c4_ttl_floor_and_default():
    from routes.bridge_routes import _pending_order_ttl_seconds
    os.environ["PENDING_ORDER_TTL_SECONDS"] = "5"
    try:
        assert _pending_order_ttl_seconds() == 30
        os.environ["PENDING_ORDER_TTL_SECONDS"] = ""
        assert _pending_order_ttl_seconds() == 120   # fix plan A2/R4
    finally:
        os.environ.pop("PENDING_ORDER_TTL_SECONDS", None)
