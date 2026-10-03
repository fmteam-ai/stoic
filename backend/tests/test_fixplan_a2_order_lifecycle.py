"""Fix plan step A2 — order lifecycle & PANIC (B2, B3/R4, B4, B7).

B2  PANIC also stops the scalp fast path (persisted config + in-process runners) and
    LOCKS the account(s); tick ingress disables a runner whose account is locked;
    /bot/start (panic_release) is the only release path.
B3  poll-trades expires ONLY orders never handed to the EA; a dispatched slow-filling
    order is never cancelled server-side. Unique partial index on (account_id, mt5_ticket)
    is refused — listing offenders — while duplicates exist.
R4  default pending-order TTL is 120s; expired never-opened orders carry close_reason="expired".
B4  kill switch / LOCKED: pending NEW orders are cancelled at the poll fence, close
    requests still flow.
B7  trade_manager tier modifications never overwrite an in-flight FULL_CLOSE.
"""
import os
import sys
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from bson import ObjectId
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "_tests_root_conftest",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "conftest.py"))
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_arun = _mod.run_async   # root conftest's shared loop (never a closed per-file loop)

TAG = "_test_fixplan_a2"


def _iso(seconds_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


@pytest.fixture
def db():
    client = MongoClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


@pytest.fixture
def seeded(db):
    user_id = f"a2-user-{ObjectId()}"
    token = f"A2-{ObjectId()}"
    acc_id = db.accounts.insert_one({
        "user_id": user_id, "label": "A2", "broker": "test", "bridge_token": token,
        "mode": "live", "trading_enabled": True, "open_positions": 0,
        "last_heartbeat": _iso(), TAG: True,
    }).inserted_id
    saved_platform = db.platform_state.find_one({"_id": "trading_authority"})
    yield {"user_id": user_id, "account_id": str(acc_id), "acc_oid": acc_id, "token": token}
    for coll in ("trades", "accounts", "scalp_configs", "bot_configs", "execution_intents"):
        db[coll].delete_many({TAG: True})
    db.close_commands.delete_many({"actor": {"$regex": "^panic:a2-user-"}})
    if saved_platform is None:
        db.platform_state.delete_one({"_id": "trading_authority"})
    else:
        db.platform_state.replace_one({"_id": "trading_authority"}, saved_platform, upsert=True)


def _pending(db, s, **extra):
    doc = {"user_id": s["user_id"], "account_id": s["account_id"], "symbol": "XAUUSD",
           "action": "BUY", "lot_size": 0.01, "entry_price": 4000, "stop_loss": 3990,
           "take_profit": 4020, "status": "pending", "mt5_ticket": None,
           "opened_at": _iso(5), TAG: True}
    doc.update(extra)
    return db.trades.insert_one(doc).inserted_id


def _poll(token):
    from routes.bridge_routes import poll_trades, PollRequest
    return _arun(poll_trades(PollRequest(bridge_token=token)))


# ------------------------------------------------------------------ B3 / R4
def test_r4_default_ttl_is_120s():
    from routes.bridge_routes import _pending_order_ttl_seconds
    saved = os.environ.pop("PENDING_ORDER_TTL_SECONDS", None)
    try:
        assert _pending_order_ttl_seconds() == 120
    finally:
        if saved is not None:
            os.environ["PENDING_ORDER_TTL_SECONDS"] = saved


def test_b3_a3_dispatched_order_expires_from_handover_never_sent_from_creation(db, seeded, monkeypatch):
    monkeypatch.setenv("PENDING_ORDER_TTL_SECONDS", "120")
    # handed to the EA 90s ago (created 10 min ago): still inside its 120s window → re-offered
    inflight = _pending(db, seeded, opened_at=_iso(600), _dispatched_at=_iso(90), _dispatch_count=1)
    # handed over 10 min ago, never confirmed → expired (A3: "like the rest")
    silent = _pending(db, seeded, opened_at=_iso(700), _dispatched_at=_iso(600), _dispatch_count=1)
    never_sent = _pending(db, seeded, opened_at=_iso(600))
    resp = _poll(seeded["token"])
    dispatched = {t["trade_id"] for t in resp["trades"]}
    assert db.trades.find_one({"_id": inflight})["status"] == "pending"
    assert str(inflight) in dispatched
    gone = db.trades.find_one({"_id": silent})
    assert gone["status"] == "cancelled" and gone["expired_after_dispatch"] is True
    stale = db.trades.find_one({"_id": never_sent})
    assert stale["status"] == "cancelled" and stale["expired_after_dispatch"] is False
    assert stale["close_reason"] == "expired" and stale["error"] == "pending_order_expired"
    assert stale["expired_after_s"] == 120
    assert not ({str(silent), str(never_sent)} & dispatched)


def test_b3_duplicate_tickets_block_unique_index_and_are_listed(db, seeded):
    from seed import duplicate_tickets, ensure_unique_ticket_index, UNIQUE_TICKET_INDEX, UNIQUE_TICKET_FILTER
    from database import get_db
    if UNIQUE_TICKET_INDEX in db.trades.index_information():
        db.trades.drop_index(UNIQUE_TICKET_INDEX)      # exercise the pre-check path from scratch
    a = _pending(db, seeded, status="open", mt5_ticket=990001)
    b = _pending(db, seeded, status="pending", mt5_ticket=990001)
    _pending(db, seeded, status="closed", mt5_ticket=990001)   # A3: closed rows never count
    dups = _arun(duplicate_tickets(get_db()))
    hit = [d for d in dups if d["mt5_ticket"] == 990001 and d["account_id"] == seeded["account_id"]]
    assert hit and set(hit[0]["trade_ids"]) == {str(a), str(b)} and hit[0]["count"] == 2
    out = _arun(ensure_unique_ticket_index(get_db()))
    assert out["created"] is False and out["duplicates"]
    db.trades.delete_one({"_id": b})
    out = _arun(ensure_unique_ticket_index(get_db()))
    remaining = _arun(duplicate_tickets(get_db()))
    assert out["created"] == (not remaining)
    if out["created"]:
        info = db.trades.index_information()[UNIQUE_TICKET_INDEX]
        assert info["unique"] is True and info["partialFilterExpression"] == UNIQUE_TICKET_FILTER
        _pending(db, seeded)                      # null tickets exempt
        _pending(db, seeded, mt5_ticket=0)
        _pending(db, seeded, status="closed", mt5_ticket=990001)   # closed re-use allowed
        # A3 netting: same position ticket, distinct entry legs coexist while open
        _pending(db, seeded, status="open", mt5_ticket=990002, position_leg=1)
        _pending(db, seeded, status="open", mt5_ticket=990002, position_leg=2)
        from pymongo.errors import DuplicateKeyError
        with pytest.raises(DuplicateKeyError):
            _pending(db, seeded, status="open", mt5_ticket=990001)


# ------------------------------------------------------------------ B4
def test_b4_platform_lock_cancels_new_orders_but_closes_still_flow(db, seeded):
    db.platform_state.update_one({"_id": "trading_authority"},
                                 {"$set": {"level": "LOCKED", "reason": "kill switch (test)"}}, upsert=True)
    intent_id = f"a2-{ObjectId()}"
    db.execution_intents.insert_one({"intent_id": intent_id, "status": "submitted", TAG: True})
    new_id = _pending(db, seeded, execution_intent_id=intent_id)
    close_id = _pending(db, seeded, close_requested=True, mt5_ticket=990100)
    resp = _poll(seeded["token"])
    ids = {t["trade_id"] for t in resp["trades"]}
    assert str(new_id) not in ids and str(close_id) in ids
    row = db.trades.find_one({"_id": new_id})
    assert row["status"] == "cancelled" and row["close_reason"] == "authority_locked"
    assert row["error"] == "authority_locked:platform_locked"
    assert db.execution_intents.find_one({"intent_id": intent_id})["status"] == "cancelled"


def test_b4_account_lock_cancels_new_orders(db, seeded):
    db.platform_state.update_one({"_id": "trading_authority"}, {"$set": {"level": "FULL"}}, upsert=True)
    db.accounts.update_one({"_id": seeded["acc_oid"]},
                           {"$set": {"trading_authority": "LOCKED", "authority_lock": {"reason": "panic"}}})
    new_id = _pending(db, seeded)
    resp = _poll(seeded["token"])
    assert str(new_id) not in {t["trade_id"] for t in resp["trades"]}
    row = db.trades.find_one({"_id": new_id})
    assert row["status"] == "cancelled" and row["error"] == "authority_locked:account_locked:panic"


def test_b4_unlocked_account_still_dispatches(db, seeded):
    db.platform_state.update_one({"_id": "trading_authority"}, {"$set": {"level": "FULL"}}, upsert=True)
    new_id = _pending(db, seeded)
    resp = _poll(seeded["token"])
    assert str(new_id) in {t["trade_id"] for t in resp["trades"]}


# ------------------------------------------------------------------ B2
def test_b2_panic_stops_scalp_and_locks_accounts(db, seeded, monkeypatch):
    import routes.panic_routes as pr
    from scalp.engine import get_runner, _runners
    monkeypatch.setattr(pr.ws_manager, "broadcast", AsyncMock())
    db.scalp_configs.insert_one({"account_id": seeded["account_id"], "user_id": seeded["user_id"],
                                 "symbol": "EURUSD", "enabled": True, "mode": "live", TAG: True})
    db.bot_configs.insert_one({"user_id": seeded["user_id"], "account_id": seeded["account_id"],
                               "active": True, TAG: True})
    runner = get_runner(seeded["account_id"], seeded["user_id"], "EURUSD")
    runner.enabled = True
    try:
        out = _arun(pr._disable_all_bots_and_close_trades({"user_id": seeded["user_id"]},
                                                           broadcast_user_id=seeded["user_id"]))
        assert out["scalp_runners_disabled"] == 1 and out["accounts_locked"] == 1
        assert runner.enabled is False
        cfg = db.scalp_configs.find_one({"account_id": seeded["account_id"], "symbol": "EURUSD"})
        assert cfg["enabled"] is False and cfg["panic_disabled_at"]
        acc = db.accounts.find_one({"_id": seeded["acc_oid"]})
        assert acc["trading_authority"] == "LOCKED" and acc["authority_lock"]["reason"] == "panic"
        # the authority snapshot explains the lock
        from trading_authority import account_domain
        dom = _arun(account_domain(None, acc))
        assert dom["level"] == "LOCKED" and "PANIC" in dom["reason"]
        # B4 follows: a queued order after PANIC never reaches the EA
        new_id = _pending(db, seeded)
        resp = _poll(seeded["token"])
        assert str(new_id) not in {t["trade_id"] for t in resp["trades"]}
        assert db.trades.find_one({"_id": new_id})["status"] == "cancelled"
    finally:
        _runners.pop(f"{seeded['account_id']}:EURUSD", None)
        db.ops_outbox.delete_many({"user_id": seeded["user_id"]})


def test_b2_tick_ingress_disables_runner_on_locked_account(db, seeded, monkeypatch):
    from routes.bridge_routes import receive_ticks, BridgeTicks
    import routes.bridge_routes as br
    from scalp.engine import get_runner, _runners
    db.accounts.update_one({"_id": seeded["acc_oid"]},
                           {"$set": {"trading_authority": "LOCKED", "authority_lock": {"reason": "panic"}}})
    runner = get_runner(seeded["account_id"], seeded["user_id"], "EURUSD")
    runner.enabled = True
    runner._hydrated = True
    ingest = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(runner, "ingest", ingest)
    monkeypatch.setattr(br, "_record_tick_ingress", AsyncMock())
    try:
        with patch("scalp.engine.ensure_account_lease", AsyncMock(return_value=True)):
            _arun(receive_ticks(BridgeTicks(bridge_token=seeded["token"], symbol="EURUSD",
                                            ticks=[], sent_at_ms=0)))
        assert runner.enabled is False
        ingest.assert_awaited()   # closes / reconciliation still flow through ingest
    finally:
        _runners.pop(f"{seeded['account_id']}:EURUSD", None)


def test_b2_bot_start_releases_panic_lock_with_step_up(db, seeded, monkeypatch):
    import routes.bot_routes as botr
    from fastapi import Request
    db.accounts.update_one({"_id": seeded["acc_oid"]},
                           {"$set": {"trading_authority": "LOCKED",
                                     "authority_lock": {"reason": "panic", "at": _iso()}}})
    db.bot_configs.insert_one({"user_id": seeded["user_id"], "account_id": seeded["account_id"],
                               "active": False, "tripped_at": _iso(), TAG: True})
    actions = []

    async def _step_up(db_, user, request, action):
        actions.append(action)
    monkeypatch.setattr(botr, "require_step_up", _step_up)
    monkeypatch.setattr(botr, "audit_event", AsyncMock())
    monkeypatch.setattr(botr, "_activation_readiness", AsyncMock(return_value=[]))
    with patch("routes.validation_routes.live_stage_gate", AsyncMock(return_value=None)):
        req = Request({"type": "http", "headers": [], "method": "POST", "path": "/bot/start",
                       "query_string": b"", "client": ("127.0.0.1", 1)})
        out = _arun(botr.start_bot(req, account_id=seeded["account_id"],
                                   user={"id": seeded["user_id"], "role": "user"}))
    assert out["active"] is True and actions == ["panic_release"]
    acc = db.accounts.find_one({"_id": seeded["acc_oid"]})
    assert "trading_authority" not in acc and "authority_lock" not in acc
    assert acc["authority_lock_released"]["via"] == "bot_start"
    # scalp stays OFF — it must be re-enabled explicitly
    assert not db.scalp_configs.find_one({"account_id": seeded["account_id"], "enabled": True})


# ------------------------------------------------------------------ B7
def test_b7_partial_close_never_overwrites_inflight_full_close(db, seeded):
    import trade_manager as tm
    full_close = {"type": "FULL_CLOSE", "requested_at": _iso(), "intent_id": "x", "seq": 3}
    tid = _pending(db, seeded, status="open", mt5_ticket=990200, original_lot_size=0.10,
                   lot_size=0.10, entry_price=4000, close_requested=True,
                   close_reason="panic", pending_modification=full_close)
    # STALE snapshot read before PANIC landed: no close, no modification yet
    snapshot = db.trades.find_one({"_id": tid})
    snapshot.pop("pending_modification")
    snapshot.pop("close_requested")
    cfg = {"user_id": seeded["user_id"], "let_winners_run": False}
    with patch.object(tm, "get_quote", AsyncMock(return_value={"price": 4000 + 100 * 0.1})), \
            patch.object(tm.ws_manager, "broadcast", AsyncMock()) as bc:
        _arun(tm._manage_one_trade(snapshot, cfg))
        bc.assert_not_awaited()
    row = db.trades.find_one({"_id": tid})
    assert row["pending_modification"] == full_close, "FULL_CLOSE must survive"
    assert row["close_requested"] is True and "tp1_target_lot" not in row


def test_b7_free_slot_still_takes_tier1_partial(db, seeded):
    import trade_manager as tm
    tid = _pending(db, seeded, status="open", mt5_ticket=990201, original_lot_size=0.10,
                   lot_size=0.10, entry_price=4000)
    snapshot = db.trades.find_one({"_id": tid})
    cfg = {"user_id": seeded["user_id"], "let_winners_run": False}
    with patch.object(tm, "get_quote", AsyncMock(return_value={"price": 4000 + 100 * 0.1})), \
            patch.object(tm.ws_manager, "broadcast", AsyncMock()) as bc:
        _arun(tm._manage_one_trade(snapshot, cfg))
        bc.assert_awaited_once()
    row = db.trades.find_one({"_id": tid})
    assert row["pending_modification"]["type"] == "PARTIAL_CLOSE" and row["tp1_target_lot"] == 0.05
