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
                                 "equity": 10_000.0, "balance": 10_000.0, "mode": "live", "account_type": "standard",
                                 "ea_version": "1.66", "last_heartbeat": datetime.now(timezone.utc).isoformat()}))
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
    # 301 s with the EA OFFLINE (stale heartbeat): kept pending — an offline terminal is not a refusal (#44)
    _run(db.accounts.update_one({"_id": world["acc_id"]}, {"$set": {"last_heartbeat": (requested - timedelta(minutes=10)).isoformat()}}))
    out = _run(cw.sweep(db, requested + timedelta(seconds=301)))
    assert out["expired"] == 0 and out["kept_offline"] == 1
    assert _run(db.trades.find_one({"_id": world["tid"]}))["close_requested"] is True
    # 301 s with the EA ONLINE: expired — close released, FULL_CLOSE cleared, ledger row expired, alert + pulse, slot free
    _run(db.accounts.update_one({"_id": world["acc_id"]}, {"$set": {"last_heartbeat": (requested + timedelta(seconds=300)).isoformat()}}))
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


# ------------------------------------------------------------------------------------------------ v1.60.55 #44 / #48
def test_mandatory_close_is_never_expired_and_is_redispatched_with_backoff(world):
    """#44 — a friday_flat close the terminal never acks: no expiry (even past 5 min, even EA online), re-dispatched as a
    fresh fenced FULL_CLOSE at 60 s, then 120 s later, … until the position is gone."""
    import close_watchdog as cw
    from close_commands import request_close
    db = world["db"]
    _run(request_close(db, {"_id": world["tid"]}, reason="friday_flat", actor="friday_flat",
                       pending_modification={"type": "FULL_CLOSE", "reason": "friday_flat"}, emergency=True))
    t0 = _run(db.trades.find_one({"_id": world["tid"]}))
    key0 = t0["close_idem_key"]
    requested = datetime.fromisoformat(t0["close_command"]["requested_at"])
    out = _run(cw.sweep(db, requested + timedelta(seconds=61)))
    assert out["alerted"] == 1 and out["redispatched"] == 1 and out["expired"] == 0
    t1 = _run(db.trades.find_one({"_id": world["tid"]}))
    assert t1["close_requested"] is True and t1["close_idem_key"] != key0 and t1["close_reason"] == "friday_flat"
    assert t1["pending_modification"]["type"] == "FULL_CLOSE" and t1["pending_modification"]["redispatch_of"] == key0
    assert t1["close_redispatch"]["count"] == 1
    assert _run(db.close_commands.find_one({"_id": f"{key0}:{world['tid']}"}))["state"] == "superseded"
    # 61 s + 100 s: backoff (120 s) not yet due → nothing; +130 s → second re-dispatch; never expired
    assert _run(cw.sweep(db, requested + timedelta(seconds=161)))["redispatched"] == 0
    out = _run(cw.sweep(db, requested + timedelta(seconds=200)))
    assert out["redispatched"] == 1 and out["expired"] == 0
    assert _run(db.trades.find_one({"_id": world["tid"]}))["close_redispatch"]["count"] == 2
    out = _run(cw.sweep(db, requested + timedelta(seconds=3600)))
    assert out["expired"] == 0 and _run(db.trades.find_one({"_id": world["tid"]}))["close_requested"] is True


def test_friday_flat_takes_over_an_unacked_var_cap_close_and_reissues(world, monkeypatch):
    """#48 — a VaR-flagged (unacked) position used to be EXCLUDED by friday_flat and stayed open over the weekend."""
    import friday_flat as ff
    db = world["db"]
    _deleverage(world)                                        # silent var_cap close: close_requested, no ack
    monkeypatch.setattr(ff, "in_friday_flat_window", lambda cfg, now=None: {"in_window": True, "close_at": "x"})

    async def _no_notify(*a, **k):
        return None
    monkeypatch.setattr("notifier.notify_friday_flat", _no_notify, raising=False)
    cfg = {"user_id": world["uid"], "account_id": str(world["acc_id"]), "friday_flat_mode": "close"}
    stats = _run(ff.sweep_user(db, world["uid"], cfg))
    assert stats["closed"] == 1
    t = _run(db.trades.find_one({"_id": world["tid"]}))
    assert t["close_reason"] == "friday_flat" and t["pending_modification"]["type"] == "FULL_CLOSE"
    assert t["pending_modification"]["reason"] == "friday_flat" and t["friday_flat_at"]
    # same tick again: our own request is pending and the backoff is not due → no duplicate command
    assert _run(ff.sweep_user(db, world["uid"], cfg))["closed"] == 0
    # 2 min later, still open and unconfirmed → re-issued (count 1)
    key1 = t["close_idem_key"]
    _run(db.trades.update_one({"_id": world["tid"]}, {"$set": {"close_command.requested_at": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()}}))
    stats = _run(ff.sweep_user(db, world["uid"], cfg))
    t2 = _run(db.trades.find_one({"_id": world["tid"]}))
    assert stats.get("reissued") == 1 and t2["close_idem_key"] != key1 and t2["close_redispatch"]["count"] == 1
    # a close the terminal has ACKED is left alone
    from close_commands import record_terminal_ack
    _run(record_terminal_ack(db, t2, success=True))
    assert _run(ff.sweep_user(db, world["uid"], cfg))["closed"] == 0


def test_eod_flatten_takes_over_unacked_close(world, monkeypatch):
    import eod_flatten as eod
    db = world["db"]
    _deleverage(world)
    monkeypatch.setattr(eod, "in_flatten_window", lambda offset, now: True)
    monkeypatch.setattr(eod, "_enabled", lambda: True, raising=False)
    out = _run(eod.sweep_eod_flatten(db))
    t = _run(db.trades.find_one({"_id": world["tid"]}))
    assert out["queued"] >= 1 and t["close_reason"] == "eod_flatten" and t["pending_modification"]["reason"] == "eod_flatten"
    assert _run(eod.sweep_eod_flatten(db))["queued"] == 0            # backoff: no duplicate on the next tick


def test_sec001_terminal_failure_ack_keeps_a_panic_close_pending_and_redispatched(world):
    """SEC-001 — a (buggy/forged) terminal FAILURE ack on a MANDATORY close must not release it: the request stays
    pending and the watchdog re-dispatches until the broker OUT deal / position gone."""
    import close_watchdog as cw
    from close_commands import record_terminal_ack, request_close
    db = world["db"]
    _run(request_close(db, {"_id": world["tid"]}, reason="panic", actor="panic:test",
                       pending_modification={"type": "FULL_CLOSE", "reason": "panic"}, emergency=True))
    t = _run(db.trades.find_one({"_id": world["tid"]}))
    key0 = t["close_idem_key"]
    ack = _run(record_terminal_ack(db, t, success=False, error="position_not_found", terminal=True))
    assert ack["kept_mandatory"] is True and ack["success"] is False
    t1 = _run(db.trades.find_one({"_id": world["tid"]}))
    assert t1["close_requested"] is True and t1["pending_modification"]["type"] == "FULL_CLOSE"
    assert t1["close_command"]["state"] == "requested" and "close_request_expired" not in t1
    assert _run(db.close_commands.find_one({"_id": f"{key0}:{world['tid']}"}))["state"] == "requested"
    requested = datetime.fromisoformat(t1["close_command"]["requested_at"])
    out = _run(cw.sweep(db, requested + timedelta(seconds=61)))
    assert out["redispatched"] == 1 and out["expired"] == 0
    assert _run(db.trades.find_one({"_id": world["tid"]}))["close_idem_key"] != key0


def test_sec001_forged_success_ack_pauses_redispatch_only_for_the_grace_window(world):
    """SEC-001 — a 'closed' ack with no broker OUT deal: no re-dispatch inside CLOSE_ACK_GRACE_SEC, re-dispatch
    resumes after it while the position is still open; mandatory_close takeover resumes too."""
    import close_watchdog as cw
    from close_commands import ACK_GRACE_SEC, mandatory_close, record_terminal_ack, request_close
    db = world["db"]
    _run(request_close(db, {"_id": world["tid"]}, reason="friday_flat", actor="friday_flat",
                       pending_modification={"type": "FULL_CLOSE", "reason": "friday_flat"}, emergency=True))
    t = _run(db.trades.find_one({"_id": world["tid"]}))
    requested = datetime.fromisoformat(t["close_command"]["requested_at"])
    ack_at = requested + timedelta(seconds=5)
    _run(record_terminal_ack(db, t, success=True, at=ack_at.isoformat()))
    t = _run(db.trades.find_one({"_id": world["tid"]}))
    key0 = t["close_idem_key"]
    inside = ack_at + timedelta(seconds=ACK_GRACE_SEC - 10)
    assert _run(cw.sweep(db, inside))["redispatched"] == 0
    assert _run(mandatory_close(db, t, reason="friday_flat", actor="friday_flat", now=inside)) == "in_flight"
    after = ack_at + timedelta(seconds=ACK_GRACE_SEC + 10)
    out = _run(cw.sweep(db, after))
    assert out["redispatched"] == 1 and out["expired"] == 0
    t2 = _run(db.trades.find_one({"_id": world["tid"]}))
    assert t2["close_idem_key"] != key0 and t2["close_requested"] is True and t2["close_redispatch"]["count"] == 1


def test_v1_60_56_redispatch_reuses_one_alert_then_escalates_and_pauses_in_quiet_window(world):
    """#49 — an EA offline for hours: ONE high alert per trade+reason (occurrences grow), a critical rung at 15 min and
    at 1 h, no alert/re-send while the broker clock is inside the EA's EOD quiet window."""
    import close_watchdog as cw
    from close_commands import request_close
    db = world["db"]
    _run(request_close(db, {"_id": world["tid"]}, reason="friday_flat", actor="friday_flat",
                       pending_modification={"type": "FULL_CLOSE", "reason": "friday_flat"}, emergency=True))
    t0 = _run(db.trades.find_one({"_id": world["tid"]}))
    requested = datetime.fromisoformat(t0["close_command"]["requested_at"])
    # broker clock pinned so that requested + 2 h reads 23:50 broker time (quiet) and every earlier probe does not
    quiet_probe = requested + timedelta(hours=2)
    want = quiet_probe.replace(hour=23, minute=50, second=0, microsecond=0)
    offset = int((want - quiet_probe.replace(second=0, microsecond=0)).total_seconds())
    _run(db.accounts.update_one({"_id": world["acc_id"]}, {"$set": {"broker_utc_offset_sec": offset}}))
    tid = str(world["tid"])
    q = {"meta.trade_id": tid, "kind": "close_command_unacked"}
    out = _run(cw.sweep(db, requested + timedelta(seconds=61)))
    assert out["alerted"] == 1 and out["redispatched"] == 1
    # 200 s: second re-dispatch → the SAME alert row (dedup per trade+reason), no new HIGH alert
    out = _run(cw.sweep(db, requested + timedelta(seconds=200)))
    assert out["redispatched"] == 1 and out["alerted"] == 0
    rows = _run(db.ops_alerts.find(q).to_list(length=20))
    assert len(rows) == 1 and rows[0]["severity"] == "warning" and rows[0]["occurrences"] >= 1   # "high" → warning tier; critical rungs below
    # 16 min: critical 15m rung, exactly once even on the next sweep
    out = _run(cw.sweep(db, requested + timedelta(seconds=16 * 60)))
    assert out["escalated"] == 1
    _run(cw.sweep(db, requested + timedelta(seconds=17 * 60)))
    rows = _run(db.ops_alerts.find(q).to_list(length=20))
    assert sorted(r.get("meta", {}).get("escalation") or "" for r in rows) == ["", "15m"]
    assert _run(db.trades.find_one({"_id": world["tid"]}))["close_unacked_escalations"] == ["15m"]
    # 61 min: 1h rung
    assert _run(cw.sweep(db, requested + timedelta(seconds=61 * 60)))["escalated"] == 1
    assert len(_run(db.ops_alerts.find(q).to_list(length=20))) == 3
    # 23:50 broker time: quiet window → nothing re-sent, nothing alerted, counted as quiet
    out = _run(cw.sweep(db, quiet_probe))
    assert out["quiet"] == 1 and out["redispatched"] == 0 and out["alerted"] == 0 and out["escalated"] == 0
    assert len(_run(db.ops_alerts.find(q).to_list(length=20))) == 3
    # 00:10 broker time: window over → re-dispatch resumes, still never expired
    out = _run(cw.sweep(db, quiet_probe + timedelta(minutes=20)))
    assert out["redispatched"] == 1 and out["expired"] == 0


def test_v1_60_56_friday_flat_and_eod_do_not_fight(world, monkeypatch):
    """#50 — Friday ~20:15 UTC both windows are open: eod_flatten must not re-issue friday_flat's 10 s old command and
    vice-versa; the trade keeps ONE pending mandatory command with the stronger reason."""
    import eod_flatten as eod
    import friday_flat as ff
    db = world["db"]
    _run(db.accounts.update_one({"_id": world["acc_id"]}, {"$set": {"broker_utc_offset_sec": 3 * 3600}}))
    monkeypatch.setattr(ff, "in_friday_flat_window", lambda cfg, now=None: {"in_window": True, "close_at": "x"})
    monkeypatch.setattr(eod, "in_flatten_window", lambda offset, now: True)
    monkeypatch.setattr(eod, "_enabled", lambda: True, raising=False)

    async def _no_notify(*a, **k):
        return None
    monkeypatch.setattr("notifier.notify_friday_flat", _no_notify, raising=False)
    cfg = {"user_id": world["uid"], "account_id": str(world["acc_id"]), "friday_flat_mode": "close"}
    assert _run(ff.sweep_user(db, world["uid"], cfg))["closed"] == 1
    t = _run(db.trades.find_one({"_id": world["tid"]}))
    key = t["close_idem_key"]
    for _ in range(3):
        assert _run(eod.sweep_eod_flatten(db))["queued"] == 0          # backoff holds across callers
        assert _run(ff.sweep_user(db, world["uid"], cfg))["closed"] == 0
    t2 = _run(db.trades.find_one({"_id": world["tid"]}))
    assert t2["close_idem_key"] == key and t2["close_reason"] == "friday_flat"
    assert _run(db.close_commands.count_documents({"trade_id": str(world["tid"]), "state": "requested"})) == 1
    # re-issue due (2 min later) from eod_flatten keeps the STRONGER friday_flat reason
    _run(db.trades.update_one({"_id": world["tid"]}, {"$set": {"close_command.requested_at": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()}}))
    assert _run(eod.sweep_eod_flatten(db))["queued"] == 1
    t3 = _run(db.trades.find_one({"_id": world["tid"]}))
    assert t3["close_idem_key"] != key and t3["close_reason"] == "friday_flat" and t3["pending_modification"]["reason"] == "friday_flat"
    assert t3["close_redispatch"]["count"] == 1 and t3["close_redispatch"]["first_at"]
