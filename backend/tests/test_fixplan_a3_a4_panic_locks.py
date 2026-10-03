"""Fix plan steps A3 (ticket index & order expiry) and A4 (PANIC, locks, exit guards).

A3  live-only unique ticket index keyed by (account, ticket, position_leg); duplicate 'in'
    deal merges into the existing row; dispatched orders expire 120s after hand-over
    (see test_fixplan_a2); --archive plan refuses risky groups.
A4  a fill after PANIC is closed at once; users release only their own panic (admin-wide
    needs /admin/panic/release); demo-only users without MFA restart without step-up;
    dashboard / Telegram / copilot show the lock; TP1 banked P&L survives a reconciler close
    and the later exact out-deal.
"""
import os
import sys
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import asyncio as _asyncio


def _arun(coro):
    """Run on the loop the root conftest installed for this test (asyncio.get_event_loop()),
    recreating it if a previous suite closed it, and never reuse a motor client bound to
    another loop. Works stand-alone and inside mixed runs."""
    import database as _dbmod
    try:
        loop = _asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or loop.is_closed():
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
    import sys as _sys
    for _m in [_dbmod] + [m for n, m in list(_sys.modules.items())
                          if (n == "database" or n.endswith(".database")) and hasattr(m, "_client")]:
        c = getattr(_m, "_client", None)
        if c is not None and c.get_io_loop() is not loop:
            _m._client = None
            _m._db = None
    return loop.run_until_complete(coro)

TAG = "_test_fixplan_a34"


def _iso(seconds_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


@pytest.fixture
def db():
    client = MongoClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


@pytest.fixture
def seeded(db):
    user_oid = ObjectId()
    user_id = str(user_oid)
    db.users.insert_one({"_id": user_oid, "email": f"a34-{user_oid}@test.local", "role": "user", TAG: True})
    token = f"A34-{ObjectId()}"
    acc_id = db.accounts.insert_one({
        "user_id": user_id, "label": "A34", "broker": "test", "bridge_token": token,
        "mode": "live", "trading_enabled": True, "open_positions": 0,
        "last_heartbeat": _iso(), TAG: True,
    }).inserted_id
    yield {"user_id": user_id, "account_id": str(acc_id), "acc_oid": acc_id, "token": token}
    for coll in ("trades", "accounts", "users", "scalp_configs", "bot_configs", "broker_deals",
                 "execution_intents", "trade_events"):
        db[coll].delete_many({TAG: True})
    db.broker_deals.delete_many({"account_id": str(acc_id)})
    db.close_commands.delete_many({"account_id": str(acc_id)})
    db.ops_outbox.delete_many({"user_id": user_id})


def _trade(db, s, **extra):
    doc = {"user_id": s["user_id"], "account_id": s["account_id"], "symbol": "XAUUSD",
           "action": "BUY", "lot_size": 0.10, "original_lot_size": 0.10, "entry_price": 4000,
           "stop_loss": 3990, "take_profit": 4020, "status": "pending", "mt5_ticket": None,
           "opened_at": _iso(5), TAG: True}
    doc.update(extra)
    return db.trades.insert_one(doc).inserted_id


def _lock(db, s, scope="user"):
    db.accounts.update_one({"_id": s["acc_oid"]}, {"$set": {
        "trading_authority": "LOCKED",
        "authority_lock": {"reason": "panic", "at": _iso(), "by": s["user_id"], "scope": scope}}})


def _req():
    from fastapi import Request
    return Request({"type": "http", "headers": [], "method": "POST", "path": "/x",
                    "query_string": b"", "client": ("127.0.0.1", 1)})


# ------------------------------------------------------------------ A3
def test_a3_index_definition_constants():
    from seed import UNIQUE_TICKET_KEYS, UNIQUE_TICKET_FILTER
    assert UNIQUE_TICKET_KEYS == [("account_id", 1), ("mt5_ticket", 1), ("position_leg", 1)]
    assert UNIQUE_TICKET_FILTER == {"mt5_ticket": {"$type": "number", "$gt": 0}, "status": {"$in": ["open", "pending"]}}


def test_a3_netting_detection_prefers_account_flag_then_registry(db, seeded):
    from routes.bridge_routes import is_netting_account
    from database import get_db
    assert _arun(is_netting_account(get_db(), {"margin_mode": "netting"})) is True
    assert _arun(is_netting_account(get_db(), {"margin_mode": "hedging", "broker_server": "x"})) is False
    with patch("broker_registry.resolve_registry",
               AsyncMock(return_value={"name": "NettingBroker", "capabilities": {"position_mode": "netting"}})):
        assert _arun(is_netting_account(get_db(), {"broker_server": "Netting-Demo"})) is True


def test_a3_duplicate_in_deal_merges_into_existing_row(db, seeded):
    from routes.bridge_routes import _merge_duplicate_in_deal
    from database import get_db
    from models import BridgeExternalDeal
    tid = _trade(db, seeded, status="open", mt5_ticket=770001, entry_price=0)
    payload = BridgeExternalDeal(bridge_token=seeded["token"], mt5_ticket=770001, deal_id=55501,
                                 deal_entry="in", symbol="XAUUSD", action="BUY", lots=0.1,
                                 price=4001.5, magic=901234, position_volume=0.2)
    merged = _arun(_merge_duplicate_in_deal(get_db(), seeded["account_id"], payload,
                                            {"entry_price": 4001.5, "origin": "auto"}))
    assert merged == str(tid)
    row = db.trades.find_one({"_id": tid})
    assert row["merged_deal_ids"] == [55501] and row["position_volume"] == 0.2
    assert row["entry_price"] == 4001.5 and row["broker_deal_id"] == 55501
    assert db.trades.count_documents({"account_id": seeded["account_id"], "mt5_ticket": 770001}) == 1
    # idempotent on retry
    _arun(_merge_duplicate_in_deal(get_db(), seeded["account_id"], payload, {}))
    assert db.trades.find_one({"_id": tid})["merged_deal_ids"] == [55501]


def test_a3_archive_plan_keeps_protected_row_and_archives_live_duplicate():
    from ops.ticket_duplicates import plan_group
    old = _iso(7200)
    bot = {"_id": 1, "status": "open", "opened_at": old, "execution_intent_id": "i1", "stop_loss": 3990}
    backfill = {"_id": 2, "status": "open", "opened_at": old, "backfilled_from_snapshot": True}
    keep, arch, why = plan_group([backfill, bot])
    assert why is None and keep["_id"] == 1 and [r["_id"] for r in arch] == [2]
    # pending + open duplicates (no intent on either): protected row wins, else newest
    keep, arch, why = plan_group([{"_id": 1, "status": "pending", "opened_at": old},
                                  {"_id": 2, "status": "open", "opened_at": _iso(3600), "stop_loss": 1}])
    assert why is None and keep["_id"] == 2 and [r["_id"] for r in arch] == [1]
    # close / modification in flight → manual
    assert plan_group([bot, {**backfill, "close_requested": True}])[2]
    assert plan_group([bot, {**backfill, "pending_modification": {"type": "MODIFY_SL"}}])[2]
    # candidate carries P&L the kept row lacks → manual
    assert "P&L" in plan_group([bot, {**backfill, "pnl": 12.5, "partial_closed": False}])[2]
    # candidate created moments ago → manual (its report may still land)
    assert "minutes" in plan_group([bot, {**backfill, "opened_at": _iso(30)}])[2]


# ------------------------------------------------------------------ A4 · late fill after PANIC
def test_a4_fill_reported_after_panic_is_closed_at_once(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeTradeReport
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    _lock(db, seeded)
    tid = _trade(db, seeded, status="cancelled", error="panic_lock", close_reason="panic",
                 closed_at=_iso(), _dispatched_at=_iso(20))
    _arun(br.report_trade(BridgeTradeReport(bridge_token=seeded["token"], trade_id=str(tid),
                                            mt5_ticket=880100, status="open", entry_price=4000.5,
                                            requested_price=4000.5)))
    row = db.trades.find_one({"_id": tid})
    assert row["status"] == "open" and row["mt5_ticket"] == 880100 and row["closed_at"] is None
    assert row["late_fill_after_cancel"] == "panic" and row["late_fill_after_panic"] is True
    assert row["close_requested"] is True and row["close_reason"] == "panic"
    assert row["pending_modification"]["type"] == "FULL_CLOSE"
    assert row["pending_modification"]["reason"] == "late_fill_after_panic"


def test_a4_unlocked_fill_is_not_closed(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeTradeReport
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    tid = _trade(db, seeded, _dispatched_at=_iso(2))
    _arun(br.report_trade(BridgeTradeReport(bridge_token=seeded["token"], trade_id=str(tid),
                                            mt5_ticket=880101, status="open", entry_price=4000.5,
                                            requested_price=4000.5)))
    row = db.trades.find_one({"_id": tid})
    assert row["status"] == "open" and not row.get("close_requested")


# ------------------------------------------------------------------ A4 · lock scope & release
def _start(seeded, monkeypatch, *, step_up_calls):
    import routes.bot_routes as botr

    async def _step_up(db_, user, request, action):
        step_up_calls.append(action)
    monkeypatch.setattr(botr, "require_step_up", _step_up)
    monkeypatch.setattr(botr, "audit_event", AsyncMock())
    monkeypatch.setattr(botr, "_activation_readiness", AsyncMock(return_value=[]))
    with patch("routes.validation_routes.live_stage_gate", AsyncMock(return_value=None)):
        return _arun(botr.start_bot(_req(), account_id=seeded["account_id"],
                                    user={"id": seeded["user_id"], "role": "user"}))


def test_a4_user_cannot_release_admin_wide_panic(db, seeded, monkeypatch):
    from fastapi import HTTPException
    _lock(db, seeded, scope="platform")
    calls = []
    with pytest.raises(HTTPException) as ei:
        _start(seeded, monkeypatch, step_up_calls=calls)
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "panic_admin_lock"
    assert db.accounts.find_one({"_id": seeded["acc_oid"]})["trading_authority"] == "LOCKED"


def test_a4_user_releases_own_panic_with_step_up(db, seeded, monkeypatch):
    _lock(db, seeded, scope="user")
    db.users.update_one({"_id": ObjectId(seeded["user_id"])}, {"$set": {"two_factor_enabled": True}})
    calls = []
    out = _start(seeded, monkeypatch, step_up_calls=calls)
    assert out["active"] is True and calls == ["panic_release"]
    acc = db.accounts.find_one({"_id": seeded["acc_oid"]})
    assert "authority_lock" not in acc and acc["authority_lock_released"]["via"] == "bot_start"


def test_g6_admin_release_targets_platform_locks_only(db, seeded, monkeypatch):
    import routes.panic_routes as pr
    _lock(db, seeded, scope="platform")
    calls = []

    async def _release(db_, query, *, actor, via):
        calls.append((query, actor, via))
        # run it scoped to the test account only — never a global write on a shared DB
        res = await db_.accounts.update_many({**query, "_id": seeded["acc_oid"], "authority_lock.reason": "panic"},
                                             {"$unset": {"trading_authority": "", "authority_lock": ""}})
        return res.modified_count
    monkeypatch.setattr(pr, "release_panic_locks", _release)
    with patch("step_up.require_step_up", AsyncMock()), patch.object(pr, "audit_event", AsyncMock()):
        out = _arun(pr.panic_release_global(_req(), user={"id": "admin1", "role": "admin"}))
    assert out["accounts_unlocked"] == 1 and calls[0][0] == pr.PLATFORM_LOCK_MATCH and calls[0][2] == "admin_panic_release"
    assert "authority_lock" not in db.accounts.find_one({"_id": seeded["acc_oid"]})


def test_a5_user_panic_never_overwrites_admin_wide_lock(db, seeded, monkeypatch):
    import routes.panic_routes as pr
    monkeypatch.setattr(pr.ws_manager, "broadcast", AsyncMock())
    _lock(db, seeded, scope="platform")
    out = _arun(pr._disable_all_bots_and_close_trades({"user_id": seeded["user_id"]}, broadcast_user_id=seeded["user_id"]))
    acc = db.accounts.find_one({"_id": seeded["acc_oid"]})
    assert acc["authority_lock"]["scope"] == "platform" and out["accounts_locked"] == 0
    # and Start still refuses
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _start(seeded, monkeypatch, step_up_calls=[])
    assert ei.value.detail["code"] == "panic_admin_lock"


def test_a4_panic_lock_scope_follows_query(db, seeded, monkeypatch):
    import routes.panic_routes as pr
    monkeypatch.setattr(pr.ws_manager, "broadcast", AsyncMock())
    # a user's own PANIC → scope=user (never run the admin-wide {} panic against a shared DB here)
    _arun(pr._disable_all_bots_and_close_trades({"user_id": seeded["user_id"]}, broadcast_user_id=seeded["user_id"]))
    assert db.accounts.find_one({"_id": seeded["acc_oid"]})["authority_lock"]["scope"] == "user"
    src = open(pr.__file__).read()
    assert '"scope": "user" if query.get("user_id") else "platform"' in src
    assert _arun(pr.release_panic_locks(pr.get_db(), {"user_id": seeded["user_id"]}, actor="t", via="t")) == 1


def test_a4_demo_user_without_mfa_restarts_without_step_up(db, seeded, monkeypatch):
    # PAPER mode is server-authoritative non-live (no broker at all)
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {"mode": "paper"}})
    _lock(db, seeded, scope="user")
    calls = []
    with patch("webauthn_mfa.has_passkey", AsyncMock(return_value=False)):
        out = _start(seeded, monkeypatch, step_up_calls=calls)
    assert out["active"] is True and calls == []
    assert "authority_lock" not in db.accounts.find_one({"_id": seeded["acc_oid"]})


def test_a4_sec001_user_set_demo_label_never_skips_step_up(db, seeded, monkeypatch):
    # SEC-001: account_type / server name / broker_environment are user-writable → still LIVE
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {
        "account_type": "demo", "broker_environment": "DEMO", "server": "Broker-Demo"}})
    _lock(db, seeded, scope="user")
    calls = []
    with patch("webauthn_mfa.has_passkey", AsyncMock(return_value=False)):
        _start(seeded, monkeypatch, step_up_calls=calls)
    assert calls == ["panic_release"]


def test_a4_live_user_without_mfa_still_needs_step_up(db, seeded, monkeypatch):
    _lock(db, seeded, scope="user")
    calls = []
    with patch("webauthn_mfa.has_passkey", AsyncMock(return_value=False)):
        _start(seeded, monkeypatch, step_up_calls=calls)
    assert calls == ["panic_release"]


# ------------------------------------------------------------------ A4 · surfaces show the lock
def test_a4_status_pulse_telegram_copilot_show_lock(db, seeded, monkeypatch):
    import routes.bot_routes as botr
    import routes.telegram_routes as tg
    import copilot
    db.bot_configs.insert_one({"user_id": seeded["user_id"], "account_id": seeded["account_id"],
                               "active": True, "symbols": ["XAUUSD"], TAG: True})
    _lock(db, seeded, scope="user")
    user = {"id": seeded["user_id"], "role": "user"}
    with patch.object(botr, "intel_window_24h", AsyncMock(return_value={})):
        st = _arun(botr.get_bot_status(account_id=seeded["account_id"], user=user))
    assert st["panic_locked"] is True and st["panic_lock"]["scope"] == "user"
    assert st["why_no_trade"].startswith("PANIC LOCK")
    pulse = _arun(botr.get_bot_pulse(user=user))
    mine = [p for p in pulse["items"] if p["account_id"] == seeded["account_id"]]
    assert mine and mine[0]["locked"] is True
    sent = []

    async def _reply(token, chat_id, text):
        sent.append(text)
    monkeypatch.setattr(tg, "_send_reply", _reply)
    _arun(tg._cmd_status("tok", 1, seeded["user_id"]))
    assert "PANIC LOCKED" in sent[-1] and "ACTIVE" not in sent[-1]
    _arun(tg._cmd_run("tok", 1, seeded["user_id"]))
    assert "PANIC LOCK active" in sent[-1]
    assert db.bot_configs.find_one({"user_id": seeded["user_id"], TAG: True})["active"] is True  # untouched
    snap = _arun(copilot._build_context_snapshot(seeded["user_id"]))
    assert snap["panic"]["active"] is True and snap["panic"]["locked_accounts"] == ["A34"]


# ------------------------------------------------------------------ A4 · TP1 banked P&L survives
def test_a4_reconciler_close_keeps_banked_tp1(db, seeded, monkeypatch):
    from trade_reconciler import reconcile_account
    tid = _trade(db, seeded, status="open", mt5_ticket=880200, partial_closed=True, pnl=40.0,
                 lot_size=0.05, live_pnl=10.0, live_price=4010.0, opened_at=_iso(600))
    unknown = _trade(db, seeded, status="open", mt5_ticket=880201, partial_closed=True, pnl=25.0,
                     lot_size=0.05, opened_at=_iso(600))
    _arun(reconcile_account(seeded["account_id"], [], source="manual"))
    row = db.trades.find_one({"_id": tid})
    assert row["status"] == "closed" and row["pnl"] == 50.0
    assert row["pnl_banked_partial"] == 40.0 and row["pnl_final_leg"] == 10.0 and row["pnl_estimated"] is True
    row2 = db.trades.find_one({"_id": unknown})
    assert row2["pnl"] == 25.0 and row2["pnl_banked_partial"] == 25.0 and row2["pnl_final_leg_unknown"] is True


def test_a4_exact_out_deal_after_reconciler_close_adds_banked_tp1(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeExternalDeal
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    tid = _trade(db, seeded, status="closed", mt5_ticket=880300, partial_closed=True, pnl=40.0,
                 pnl_banked_partial=40.0, pnl_estimated=True, lot_size=0.05, closed_at=_iso(60),
                 close_reason="broker_reconciled_heartbeat")
    _arun(br.external_deal(BridgeExternalDeal(bridge_token=seeded["token"], mt5_ticket=880300, deal_id=66601,
                                              deal_entry="out", symbol="XAUUSD", action="SELL", lots=0.05,
                                              price=4012.0, profit=12.0, magic=901234, position_volume=0.0)))
    row = db.trades.find_one({"_id": tid})
    assert row["status"] == "closed" and row["pnl"] == 52.0
    assert row["pnl_banked_partial"] == 40.0 and row["pnl_final_leg"] == 12.0


def test_a4_telegram_panic_uses_the_same_brake(db, seeded, monkeypatch):
    import routes.telegram_routes as tg
    import routes.panic_routes as pr
    monkeypatch.setattr(pr.ws_manager, "broadcast", AsyncMock())
    sent = []

    async def _reply(token, chat_id, text):
        sent.append(text)
    monkeypatch.setattr(tg, "_send_reply", _reply)
    _arun(tg._cmd_panic("tok", 1, seeded["user_id"]))
    acc = db.accounts.find_one({"_id": seeded["acc_oid"]})
    assert acc["trading_authority"] == "LOCKED" and acc["authority_lock"]["scope"] == "user"
    assert "accounts LOCKED" in sent[-1]


# ------------------------------------------------------------------ A5 · expiry & late-fill duplicates
def test_a5_reoffers_do_not_reset_the_expiry_clock(db, seeded, monkeypatch):
    """A dispatched-but-unconfirmed order is re-offered every 30s; the 120s expiry must count
    from the FIRST hand-over, so it expires while the EA stays online."""
    monkeypatch.setenv("PENDING_ORDER_TTL_SECONDS", "120")
    from routes.bridge_routes import poll_trades, PollRequest
    tid = _trade(db, seeded, opened_at=_iso(400), _dispatched_at=_iso(31), _first_dispatched_at=_iso(130),
                 _dispatch_count=4)
    fresh = _trade(db, seeded, opened_at=_iso(10))
    resp = _arun(poll_trades(PollRequest(bridge_token=seeded["token"])))
    ids = {t["trade_id"] for t in resp["trades"]}
    row = db.trades.find_one({"_id": tid})
    assert row["status"] == "cancelled" and row["expired_after_dispatch"] is True and str(tid) not in ids
    # the fresh order is handed over and gets its first-dispatch anchor; a re-offer keeps it
    f = db.trades.find_one({"_id": fresh})
    assert str(fresh) in ids and f["_first_dispatched_at"]
    db.trades.update_one({"_id": fresh}, {"$set": {"_dispatched_at": _iso(40)}})
    _arun(poll_trades(PollRequest(bridge_token=seeded["token"])))
    assert db.trades.find_one({"_id": fresh})["_first_dispatched_at"] == f["_first_dispatched_at"]


def test_a5_late_fill_absorbs_unprotected_backfill_duplicate(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeTradeReport
    from seed import ensure_unique_ticket_index
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    if not _arun(ensure_unique_ticket_index(br.get_db())).get("present"):
        pytest.skip("unique ticket index not buildable on this DB")
    # expired bot order (protected: SL/TP) + snapshot backfill row that grabbed the ticket first
    bot = _trade(db, seeded, status="cancelled", close_reason="expired", error="pending_order_expired",
                 _dispatched_at=_iso(300), execution_intent_id=f"a5-{ObjectId()}")
    dup = _trade(db, seeded, status="open", mt5_ticket=880400, stop_loss=0, take_profit=0,
                 origin="auto", backfilled_from_snapshot=True, protection_missing=True,
                 live_pnl=-3.2, live_price=3999.0)
    out = _arun(br.report_trade(BridgeTradeReport(bridge_token=seeded["token"], trade_id=str(bot),
                                                  mt5_ticket=880400, status="open", entry_price=4000.0,
                                                  requested_price=4000.0)))
    assert out.get("ok", True) is not False
    b = db.trades.find_one({"_id": bot})
    assert b["status"] == "open" and b["mt5_ticket"] == 880400 and b["stop_loss"] == 3990
    assert b["absorbed_duplicate_id"] == str(dup) and b["live_pnl"] == -3.2
    d = db.trades.find_one({"_id": dup})
    assert d["status"] == "superseded" and d["superseded_by"] == str(bot) and d["close_reason"] == "duplicate_absorbed"
    assert db.trades.count_documents({"account_id": seeded["account_id"], "mt5_ticket": 880400,
                                      "status": {"$in": ["open", "pending"]}}) == 1
    # a retry of the same report is idempotent (no DuplicateKeyError, no 500)
    _arun(br.report_trade(BridgeTradeReport(bridge_token=seeded["token"], trade_id=str(bot),
                                            mt5_ticket=880400, status="open", entry_price=4000.0,
                                            requested_price=4000.0)))


def test_a5_bot_originated_duplicate_is_not_absorbed(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeTradeReport
    from seed import ensure_unique_ticket_index
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    if not _arun(ensure_unique_ticket_index(br.get_db())).get("present"):
        pytest.skip("unique ticket index not buildable on this DB")
    a = _trade(db, seeded, _dispatched_at=_iso(5))
    other = _trade(db, seeded, status="open", mt5_ticket=880401, execution_intent_id=f"a5-{ObjectId()}")
    out = _arun(br.report_trade(BridgeTradeReport(bridge_token=seeded["token"], trade_id=str(a),
                                                  mt5_ticket=880401, status="open", entry_price=4000.0,
                                                  requested_price=4000.0)))
    assert out["ok"] is False and out["reason"] == "duplicate_ticket_conflict"
    assert db.trades.find_one({"_id": other})["status"] == "open"
    assert db.trades.find_one({"_id": a})["duplicate_ticket_conflict"]["ticket"] == 880401


# ------------------------------------------------------------------ A5 round 2 (reviewer N-items)
def test_n4_deal_before_report_adopts_expired_bot_row(db, seeded, monkeypatch):
    """External 'in' deal arrives BEFORE /bridge/report for an expired (late-filled) bot order:
    the cancelled row is adopted — no second row, and the later report lands cleanly."""
    import routes.bridge_routes as br
    from models import BridgeExternalDeal, BridgeTradeReport
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    bot = _trade(db, seeded, status="cancelled", error="pending_order_expired", close_reason="expired",
                 _dispatched_at=_iso(200), opened_at=_iso(300), closed_at=_iso(60))
    _arun(br.external_deal(BridgeExternalDeal(bridge_token=seeded["token"], mt5_ticket=880500, deal_id=77701,
                                              deal_entry="in", symbol="XAUUSD", action="BUY", lots=0.1,
                                              price=4000.5, magic=901234, position_volume=0.1)))
    row = db.trades.find_one({"_id": bot})
    assert row["status"] == "open" and row["mt5_ticket"] == 880500 and row["adopted_via_external_deal"]
    assert row["late_fill_after_cancel"] == "expired" and row["stop_loss"] == 3990 and row["closed_at"] is None
    assert db.trades.count_documents({"account_id": seeded["account_id"], "mt5_ticket": 880500}) == 1
    out = _arun(br.report_trade(BridgeTradeReport(bridge_token=seeded["token"], trade_id=str(bot),
                                                  mt5_ticket=880500, status="open", entry_price=4000.5,
                                                  requested_price=4000.5)))
    assert out.get("ok", True) is not False


def test_n11_heartbeat_before_report_adopts_pending_bot_row_and_closes_after_panic(db, seeded, monkeypatch):
    import routes.bridge_routes as br
    from models import BridgeHeartbeat, BridgePosition
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    _lock(db, seeded)
    bot = _trade(db, seeded, status="cancelled", error="panic_lock", close_reason="panic",
                 _dispatched_at=_iso(20), closed_at=_iso(10))
    hb = BridgeHeartbeat(bridge_token=seeded["token"], balance=10000, equity=10000, open_positions=1,
                         positions=[BridgePosition(ticket=880600, symbol="XAUUSD", type="BUY", volume=0.1,
                                                   price_open=4000.0, magic=901234)])
    _arun(br.heartbeat(hb))
    row = db.trades.find_one({"_id": bot})
    assert row["status"] == "open" and row["mt5_ticket"] == 880600 and row["adopted_via_heartbeat"]
    assert row["close_requested"] is True and row["pending_modification"]["reason"] == "late_fill_after_panic"
    assert db.trades.count_documents({"account_id": seeded["account_id"], "mt5_ticket": 880600}) == 1


def test_n2_position_leg_on_backfill_reversal_and_import_for_netting(db, seeded, monkeypatch):
    """A6/H5 — ONE leg rule: entry DEAL id on netting accounts; a snapshot backfill / manual
    import knows no deal and is unkeyed (never the position ticket)."""
    import routes.bridge_routes as br
    from models import BridgeHeartbeat, BridgePosition
    monkeypatch.setattr(br.ws_manager, "broadcast", AsyncMock())
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {"ea_identity": {"margin_mode": "netting"}}})
    assert _arun(br.is_netting_account(br.get_db(), db.accounts.find_one({"_id": seeded["acc_oid"]}))) is True
    hb = BridgeHeartbeat(bridge_token=seeded["token"], balance=10000, equity=10000, open_positions=1,
                         positions=[BridgePosition(ticket=880700, symbol="EURUSD", type="SELL", volume=0.2,
                                                   price_open=1.1, magic=0)])
    _arun(br.heartbeat(hb))
    row = db.trades.find_one({"account_id": seeded["account_id"], "mt5_ticket": 880700})
    assert row and row.get("position_leg") is None and row["backfilled_from_snapshot"]
    db.trades.update_one({"_id": row["_id"]}, {"$set": {TAG: True}})
    src = open(br.__file__).read()
    assert src.count("netting_leg(await is_netting_account(db, acc), payload.deal_id)") == 2   # 'in' + reversal
    assert "position_leg=netting_leg(netting_acc, payload.deal_id)" in src
    assert 'int(p.ticket)} if await is_netting_account' not in open(
        os.path.join(os.path.dirname(br.__file__), "account_routes.py")).read()


def test_n7_adaptive_exits_use_free_slot_filter():
    import adaptive_exits, inspect
    src = inspect.getsource(adaptive_exits)
    assert src.count("_free_slot_filter(trade[\"_id\"])") == 2
    assert src.count("if not res.modified_count:") == 2   # A6 — guarded write not applied → no event, False
    assert '{"_id": trade["_id"]},\n            {"$set": {"pending_modification"' not in src


def test_n9_index_build_guarded_no_scan_when_present_and_never_drops_at_boot(monkeypatch):
    """Pure: present+correct → no duplicate scan, no drop; stale → reported, NOT dropped unless rebuild=True."""
    import seed
    fake = MagicMock()
    good = {"key": seed.UNIQUE_TICKET_KEYS, "partialFilterExpression": seed.UNIQUE_TICKET_FILTER}
    fake.trades.index_information = AsyncMock(return_value={seed.UNIQUE_TICKET_INDEX: good})
    fake.trades.drop_index = AsyncMock(); fake.trades.create_index = AsyncMock()
    scan = AsyncMock(return_value=[]); monkeypatch.setattr(seed, "duplicate_tickets", scan)
    assert _arun(seed.ensure_unique_ticket_index(fake)) == {"created": False, "present": True, "duplicates": []}
    scan.assert_not_awaited(); fake.trades.drop_index.assert_not_awaited()
    stale = {"key": [("account_id", 1), ("mt5_ticket", 1)], "partialFilterExpression": {}}
    fake.trades.index_information = AsyncMock(return_value={seed.UNIQUE_TICKET_INDEX: stale})
    out = _arun(seed.ensure_unique_ticket_index(fake))
    assert out.get("stale") is True; fake.trades.drop_index.assert_not_awaited(); fake.trades.create_index.assert_not_awaited()
    out = _arun(seed.ensure_unique_ticket_index(fake, rebuild=True))
    assert out["created"] is True; fake.trades.drop_index.assert_awaited_once(); fake.trades.create_index.assert_awaited_once()


def test_n10_tick_ingress_halts_on_platform_kill_switch(db, seeded, monkeypatch):
    from routes.bridge_routes import receive_ticks, BridgeTicks
    import routes.bridge_routes as br
    from scalp.engine import get_runner, _runners
    prev = (db.platform_state.find_one({"_id": "trading_authority"}) or {}).get("level")   # H7 — restore after
    db.platform_state.update_one({"_id": "trading_authority"}, {"$set": {"level": "LOCKED"}}, upsert=True)
    runner = get_runner(seeded["account_id"], seeded["user_id"], "EURUSD")
    runner.enabled = True
    runner._hydrated = True
    monkeypatch.setattr(runner, "ingest", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(br, "_record_tick_ingress", AsyncMock())
    try:
        with patch("scalp.engine.ensure_account_lease", AsyncMock(return_value=True)):
            _arun(receive_ticks(BridgeTicks(bridge_token=seeded["token"], symbol="EURUSD", ticks=[], sent_at_ms=0)))
        assert runner.enabled is False
    finally:
        _runners.pop(f"{seeded['account_id']}:EURUSD", None)
        if prev is None:
            db.platform_state.delete_one({"_id": "trading_authority"})
        else:
            db.platform_state.update_one({"_id": "trading_authority"}, {"$set": {"level": prev}})


def test_h2_ea_demo_proof_alone_never_skips_step_up(db, seeded, monkeypatch):
    """A6/H2 — every demo_proof input is owner-controlled: a live account could pose as demo.
    Only PAPER or an admin-approved attestation (attested_environment != LIVE) skips the step-up."""
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {
        "server": "Broker-Demo", "account_number": "123", "broker_account_id_reported": "123",
        "ea_identity": {"authoritative": True, "installation_id": "inst-1", "broker_server": "Broker-Demo"},
        "last_heartbeat": _iso(5)}})
    from broker_env import demo_proof, attested_environment
    acc = db.accounts.find_one({"_id": seeded["acc_oid"]})
    assert demo_proof(acc)["ok"] is True and attested_environment(acc) == "LIVE"
    _lock(db, seeded, scope="user")
    calls = []
    with patch("webauthn_mfa.has_passkey", AsyncMock(return_value=False)):
        out = _start(seeded, monkeypatch, step_up_calls=calls)
    assert out["active"] is True and calls == ["panic_release"]


def test_h2_paper_only_scope_releases_without_step_up(db, seeded, monkeypatch):
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {"mode": "paper"}})
    _lock(db, seeded, scope="user")
    calls = []
    with patch("webauthn_mfa.has_passkey", AsyncMock(return_value=False)):
        out = _start(seeded, monkeypatch, step_up_calls=calls)
    assert out["active"] is True and calls == []


def test_n13_chat_enable_bots_refuses_while_locked(db, seeded):
    from routes.nl_routes import _enable_bots
    import state_contract
    _lock(db, seeded)
    db.bot_configs.insert_one({"user_id": seeded["user_id"], "account_id": seeded["account_id"], "active": False, TAG: True})
    out = _arun(_enable_bots(seeded["user_id"], None))
    assert out["blocked"] == "panic_lock" and out["bots_enabled"] == 0
    assert db.bot_configs.find_one({"user_id": seeded["user_id"], TAG: True})["active"] is False
    c = _arun(state_contract.contract(state_contract.get_db() if hasattr(state_contract, "get_db") else __import__("database").get_db(), seeded["user_id"]))
    mine = [r for r in c["accounts"] if r["account_id"] == seeded["account_id"]] if "accounts" in c else []
    assert not mine or mine[0].get("tripped") or "panic" in str(mine[0]).lower()


def test_n15_release_bumps_authority_version(db, seeded):
    import routes.panic_routes as pr
    from canonical_decision import authority_version
    _lock(db, seeded, scope="user")
    before = _arun(authority_version(pr.get_db()))
    assert _arun(pr.release_panic_locks(pr.get_db(), {"user_id": seeded["user_id"]}, actor="t", via="test")) == 1
    assert _arun(authority_version(pr.get_db())) > before


def test_n16_trace_compares_requested_lot_size():
    import execution_trace, inspect
    src = inspect.getsource(execution_trace)
    assert 'trade.get("requested_lot_size") or trade.get("original_lot_size")' in src


def test_g2_legacy_admin_lock_without_scope_is_platform(db, seeded, monkeypatch):
    import routes.panic_routes as pr
    from fastapi import HTTPException
    monkeypatch.setattr(pr.ws_manager, "broadcast", AsyncMock())
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {
        "trading_authority": "LOCKED", "authority_lock": {"reason": "panic", "at": _iso(), "by": "admin"}}})
    _arun(pr._disable_all_bots_and_close_trades({"user_id": seeded["user_id"]}, broadcast_user_id=seeded["user_id"]))
    assert db.accounts.find_one({"_id": seeded["acc_oid"]})["authority_lock"]["by"] == "admin"
    with pytest.raises(HTTPException) as ei:
        _start(seeded, monkeypatch, step_up_calls=[])
    assert ei.value.detail["code"] == "panic_admin_lock"
    from trading_authority import account_domain
    assert "admin-wide" in _arun(account_domain(None, db.accounts.find_one({"_id": seeded["acc_oid"]})))["reason"]


def test_g4_unlocked_live_account_in_scope_keeps_step_up(db, seeded, monkeypatch):
    # locked account is PAPER, but the default-scope start also touches an unlocked LIVE account
    db.accounts.update_one({"_id": seeded["acc_oid"]}, {"$set": {"mode": "paper"}})
    live = db.accounts.insert_one({"user_id": seeded["user_id"], "label": "LIVE", "mode": "live",
                                   "trading_enabled": True, TAG: True}).inserted_id
    _lock(db, seeded, scope="user")
    import routes.bot_routes as botr
    calls = []

    async def _step_up(db_, user, request, action):
        calls.append(action)
    monkeypatch.setattr(botr, "require_step_up", _step_up)
    monkeypatch.setattr(botr, "audit_event", AsyncMock())
    monkeypatch.setattr(botr, "_activation_readiness", AsyncMock(return_value=[]))
    with patch("routes.validation_routes.live_stage_gate", AsyncMock(return_value=None)), \
            patch("webauthn_mfa.has_passkey", AsyncMock(return_value=False)):
        _arun(botr.start_bot(_req(), account_id=None, user={"id": seeded["user_id"], "role": "user"}))
    assert calls == ["panic_release"]
    db.accounts.delete_one({"_id": live})
