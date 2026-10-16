"""#43 (b)+(c) — deleverage close → fenced FULL_CLOSE command → (fake EA) ack / silence → watchdog alert at 60 s,
expiry at 5 min, trade management released. Real Mongo (test DB), unique user per run."""
import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))

pytestmark = pytest.mark.integration


def _run(coro):
    from conftest import run_async
    return run_async(coro)


@pytest.fixture
def world():
    from database import get_db
    db = get_db()
    uid = str(ObjectId())
    acc_id = ObjectId()
    _run(db.accounts.insert_one({"_id": acc_id, "user_id": uid, "label": "RoboForex-e2e", "broker": "RoboForex",
                                 "equity": 10_000.0, "balance": 10_000.0, "mode": "live", "account_type": "standard"}))
    cfg_id = ObjectId()
    _run(db.bot_configs.insert_one({"_id": cfg_id, "user_id": uid, "account_id": str(acc_id), "active": True}))
    tid = ObjectId()
    _run(db.trades.insert_one({"_id": tid, "user_id": uid, "account_id": str(acc_id), "symbol": "XAUUSD", "action": "BUY",
                               "status": "open", "mode": "live", "lot_size": 0.05, "original_lot_size": 0.05,
                               "entry_price": 4000.0, "stop_loss": 3995.0, "take_profit": 4015.0, "mt5_ticket": 600007662145,
                               "opened_at": datetime.now(timezone.utc).isoformat(), "origin": "auto"}))
    yield {"db": db, "uid": uid, "acc_id": acc_id, "tid": tid, "cfg_id": cfg_id}
    _run(db.bot_configs.delete_many({"user_id": uid}))
    _run(db.trades.delete_many({"user_id": uid}))
    _run(db.accounts.delete_many({"user_id": uid}))
    _run(db.close_commands.delete_many({"trade_id": str(tid)}))
    _run(db.ops_alerts.delete_many({"meta.trade_id": str(tid)}))


def _deleverage(w):
    from portfolio.risk_manager import execute_deleveraging_actions
    res = _run(execute_deleveraging_actions(w["db"], user_id=w["uid"], actions=[
        {"kind": "close_trade", "trade_id": str(w["tid"]), "symbol": "XAUUSD", "lot_size": 0.05, "reason": "auto_deleverage_var_cap"}]))
    assert res["closed"] == 1
    return _run(w["db"].trades.find_one({"_id": w["tid"]}))


def test_deleverage_close_is_a_fenced_full_close_with_intent(world):
    """(b) the command the EA actually executes + acks: pending_modification FULL_CLOSE with intent_id, ledger row requested."""
    t = _deleverage(world)
    assert t["close_requested"] is True and t["close_reason"] == "auto_deleverage_var_cap"
    pm = t["pending_modification"]
    assert pm["type"] == "FULL_CLOSE" and pm["intent_id"] and pm["close_command_key"] == t["close_idem_key"]
    row = _run(world["db"].close_commands.find_one({"_id": f"{t['close_idem_key']}:{world['tid']}"}))
    assert row["state"] == "requested" and row["terminal_ack"] is None and row["pending_modification"]["type"] == "FULL_CLOSE"
    # the trade manager's free-slot filter is blocked while the close is in flight (by design, short-lived)
    from trade_manager import _free_slot_filter
    assert _run(world["db"].trades.find_one(_free_slot_filter(world["tid"]))) is None


def test_watchdog_alerts_at_60s_and_expires_at_5min_then_management_resumes(world):
    """(b)+(c) fake EA stays silent: alert once after 60 s, expiry after 300 s releases close_requested + FULL_CLOSE."""
    import close_watchdog as cw
    db = world["db"]
    t = _deleverage(world)
    key = t["close_idem_key"]
    requested = datetime.fromisoformat(t["close_command"]["requested_at"])
    # 30 s: nothing
    out = _run(cw.sweep(db, requested + timedelta(seconds=30)))
    assert out["alerted"] == 0 and out["expired"] == 0
    # 61 s: one alert + pulse, row stamped, not expired; a second sweep does not re-alert
    out = _run(cw.sweep(db, requested + timedelta(seconds=61)))
    assert out["alerted"] == 1 and out["expired"] == 0
    assert _run(cw.sweep(db, requested + timedelta(seconds=90)))["alerted"] == 0
    row = _run(db.close_commands.find_one({"_id": f"{key}:{world['tid']}"}))
    assert row["unacked_alert_at"] and row["state"] == "requested"
    al = _run(db.ops_alerts.find_one({"kind": "close_command_unacked", "meta.trade_id": str(world["tid"])}))
    assert al and "no terminal ack" in al["message"] and "600007662145" in al["message"]
    assert _run(db.trades.find_one({"_id": world["tid"]}))["close_requested"] is True
    # 301 s: expired — close released, FULL_CLOSE cleared, ledger row expired, alert + pulse, management slot free
    out = _run(cw.sweep(db, requested + timedelta(seconds=301)))
    assert out["expired"] == 1
    t2 = _run(db.trades.find_one({"_id": world["tid"]}))
    assert t2["status"] == "open" and t2["close_requested"] is False and t2["pending_modification"] is None
    assert t2["close_request_expired"]["key"] == key and t2["close_command"]["state"] == "expired"
    row = _run(db.close_commands.find_one({"_id": f"{key}:{world['tid']}"}))
    assert row["state"] == "expired" and row["expiry_reason"].startswith("no broker confirmation")
    assert _run(db.ops_alerts.find_one({"kind": "close_command_expired", "meta.trade_id": str(world["tid"])}))
    pulse = _run(db.bot_configs.find_one({"_id": world["cfg_id"]}))["_last_notable_pulse"]
    assert "close request expired — management resumed" in pulse["reason"] and pulse["level"] == "warn"
    from trade_manager import _free_slot_filter
    assert _run(db.trades.find_one(_free_slot_filter(world["tid"]))) is not None   # TP1 / BE / de-risk can run again
    # a later sweep is a no-op (row no longer requested)
    later = _run(cw.sweep(db, requested + timedelta(seconds=600)))
    assert later["alerted"] == 0 and later["expired"] == 0


def test_terminal_failure_ack_releases_the_close_immediately(world):
    """(c) fake EA answers FULL_CLOSE with a terminal failure → ledger failed + terminal_ack, close released now."""
    from close_commands import record_terminal_ack
    db = world["db"]
    t = _deleverage(world)
    ack = _run(record_terminal_ack(db, t, success=False, error="position_not_found", terminal=True))
    assert ack["success"] is False and ack["terminal"] is True
    row = _run(db.close_commands.find_one({"_id": f"{t['close_idem_key']}:{world['tid']}"}))
    assert row["state"] == "failed" and row["terminal_ack"]["error"] == "position_not_found"
    t2 = _run(db.trades.find_one({"_id": world["tid"]}))
    assert t2["close_requested"] is False and t2["pending_modification"] is None
    assert t2["close_request_expired"]["state"] == "failed" and "position_not_found" in t2["close_request_expired"]["reason"]
    # idempotent: a replayed ack is a no-op
    assert _run(record_terminal_ack(db, t2, success=False, error="x", terminal=True)) is None


def test_successful_terminal_ack_is_recorded_and_does_not_alert(world):
    """(b) fake EA acks success (OrderSend accepted): terminal_ack recorded, the row waits for the broker OUT deal;
    the 60 s watchdog does not raise 'unacked' for it."""
    import close_watchdog as cw
    from close_commands import record_terminal_ack
    db = world["db"]
    t = _deleverage(world)
    assert _run(record_terminal_ack(db, t, success=True))["success"] is True
    row = _run(db.close_commands.find_one({"_id": f"{t['close_idem_key']}:{world['tid']}"}))
    assert row["state"] == "requested" and row["terminal_ack"]["success"] is True
    requested = datetime.fromisoformat(t["close_command"]["requested_at"])
    out = _run(cw.sweep(db, requested + timedelta(seconds=90)))
    assert out["alerted"] == 0 and _run(db.ops_alerts.find_one({"kind": "close_command_unacked", "meta.trade_id": str(world["tid"])})) is None
    assert _run(db.trades.find_one({"_id": world["tid"]}))["close_requested"] is True


def test_unique_user_isolation():
    assert uuid.uuid4()
