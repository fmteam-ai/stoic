"""v1.60.56 follow-ups #49–#54 — alert/Telegram dedup + quiet-window pause, mandatory-reason precedence, netting legs,
mandatory late-fill/intentional/hard-drawdown reasons, real entry costs for break-even (BUY + SELL), optimizer guard,
sector-cap pulse dedup. Pure unit tests; the Mongo e2e lives in tests/integration/test_v1_60_54_close_watchdog.py."""
import asyncio
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------------------------------------------ #49
def test_ea_quiet_window_mirrors_broker_clock_and_heartbeat_flag():
    from close_watchdog import ea_quiet
    utc_2350 = datetime(2026, 10, 16, 23, 50, tzinfo=timezone.utc)
    assert ea_quiet({"broker_utc_offset_sec": 0}, utc_2350) is True                     # 23:50 broker → quiet
    assert ea_quiet({"broker_utc_offset_sec": 3 * 3600}, utc_2350) is False             # 02:50 broker → open
    assert ea_quiet({"broker_utc_offset_sec": 3 * 3600}, datetime(2026, 10, 16, 20, 45, tzinfo=timezone.utc)) is True  # 23:45
    assert ea_quiet({"ea_quiet_window": True}, datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)) is True
    assert ea_quiet(None, datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)) is False


def test_watchdog_alerts_are_deduped_per_trade_reason_and_escalate_15m_1h():
    import close_watchdog as cw
    src = inspect.getsource(cw.sweep)
    assert 'dedup_key=f"close_unacked:{tid}:{row.get(\'reason\')}"' in src          # per trade+reason, not per row
    assert "if new is not None:" in src                                               # pulse only on a NEW alert
    assert "ea_quiet(acc, now)" in src and 'out["quiet"] += 1' in src                # quiet window → no re-send, no alert
    assert cw.ESCALATION_STEPS == ((900, "15m", "critical"), (3600, "1h", "critical"))
    esc = inspect.getsource(cw._escalate)
    assert 'dedup_key=f"close_unacked:{tid}:{reason}:{label}"' in esc and "close_unacked_escalations" in esc
    rd = inspect.getsource(cw._redispatch)
    assert "if count == 0:" in rd                                                     # pulse once per trade


def test_friday_flat_and_eod_notify_once_per_trade():
    ff = open(os.path.join(BACKEND, "friday_flat.py")).read()
    assert "first_touch = not tr.get(\"friday_flat_at\")" in ff and "if first_touch:\n                actions.append" in ff
    assert '"quiet"' in ff                                                            # quiet outcome is not counted
    eod = open(os.path.join(BACKEND, "eod_flatten.py")).read()
    assert "acct_new += 1 if outcome == \"issued\" else 0" in eod and "and acct_new:" in eod


def test_mandatory_close_defers_reissue_in_quiet_window_but_not_first_issue(monkeypatch):
    import close_commands as cc
    sent = []

    async def _rc(db, q, **kw):
        sent.append(kw)
        return {"trades_marked_for_close": 1}
    monkeypatch.setattr(cc, "request_close", _rc)
    quiet_acc = {"broker_utc_offset_sec": 0}
    now = datetime(2026, 10, 16, 23, 50, tzinfo=timezone.utc)
    fresh = {"_id": "t", "status": "open"}                                            # first issue → allowed
    assert _run(cc.mandatory_close(None, fresh, reason="eod_flatten", actor="eod", now=now, account=quiet_acc)) == "issued"
    pending = {"_id": "t", "close_requested": True, "close_redispatch": {"count": 0},
               "close_command": {"state": "requested", "reason": "eod_flatten", "key": "k0",
                                 "requested_at": (now - timedelta(seconds=600)).isoformat()}}
    assert _run(cc.mandatory_close(None, pending, reason="eod_flatten", actor="eod", now=now, account=quiet_acc)) == "quiet"
    assert _run(cc.mandatory_close(None, pending, reason="eod_flatten", actor="eod", now=now, account={"broker_utc_offset_sec": 7200})) == "reissued"
    assert len(sent) == 2


# ------------------------------------------------------------------------------------------------------ #50
def test_strongest_mandatory_reason_precedence():
    from close_watchdog import reason_rank, strongest_reason
    assert reason_rank("panic") < reason_rank("pre_news_protect") < reason_rank("friday_flat") < reason_rank("eod_flatten") < reason_rank("manual")
    assert strongest_reason("eod_flatten", "friday_flat") == "friday_flat"
    assert strongest_reason("friday_flat", "eod_flatten") == "friday_flat"
    assert strongest_reason("friday_flat", "panic") == "panic" and strongest_reason("panic", "friday_flat") == "panic"
    assert strongest_reason("manual_telegram", "eod_flatten") == "eod_flatten"
    assert strongest_reason("friday_flat", "friday_flat") == "friday_flat"


def test_any_pending_mandatory_reason_counts_as_own_and_keeps_the_strongest(monkeypatch):
    """friday_flat + eod_flatten overlap on Fridays: neither re-issues the other's command every tick (backoff holds),
    and when a re-issue IS due the stronger reason is kept; a pending panic is never overwritten by friday_flat."""
    import close_commands as cc
    sent = []

    async def _rc(db, q, **kw):
        sent.append(kw)
        return {"trades_marked_for_close": 1}
    monkeypatch.setattr(cc, "request_close", _rc)
    now = datetime(2026, 10, 16, 20, 16, tzinfo=timezone.utc)
    acc = {"broker_utc_offset_sec": 3 * 3600}                                         # 23:16 broker — flatten window, not quiet

    def _pending(reason, age):
        return {"_id": "t", "close_requested": True, "close_redispatch": {"count": 0},
                "close_command": {"state": "requested", "reason": reason, "key": "k0",
                                  "requested_at": (now - timedelta(seconds=age)).isoformat()}}
    # eod_flatten meets a 10 s old friday_flat request → backoff (own), NOT a takeover
    assert _run(cc.mandatory_close(None, _pending("friday_flat", 10), reason="eod_flatten", actor="eod", now=now, account=acc)) == "backoff"
    assert _run(cc.mandatory_close(None, _pending("eod_flatten", 10), reason="friday_flat", actor="ff", now=now, account=acc)) == "backoff"
    assert not sent
    # 70 s old → re-issue due; the STRONGER reason survives in both directions
    assert _run(cc.mandatory_close(None, _pending("friday_flat", 70), reason="eod_flatten", actor="eod", now=now, account=acc)) == "reissued"
    assert sent[-1]["reason"] == "friday_flat" and sent[-1]["pending_modification"]["reason"] == "friday_flat"
    assert _run(cc.mandatory_close(None, _pending("panic", 70), reason="friday_flat", actor="ff", now=now, account=acc,
                                   stamp={"close_reason_pending": "friday_flat"})) == "reissued"
    assert sent[-1]["reason"] == "panic" and sent[-1]["stamp"]["close_reason_pending"] == "panic"
    assert sent[-1]["stamp"]["close_redispatch"]["count"] == 1 and sent[-1]["stamp"]["close_redispatch"]["first_at"]
    # a DISCRETIONARY pending request (var_cap) is still taken over immediately
    assert _run(cc.mandatory_close(None, _pending("auto_deleverage_var_cap", 5), reason="friday_flat", actor="ff", now=now, account=acc)) == "issued"
    assert sent[-1]["reason"] == "friday_flat"


# ------------------------------------------------------------------------------------------------------ #51
def test_acked_netting_legs_are_never_redispatched(monkeypatch):
    import close_commands as cc
    import close_watchdog as cw
    called = []

    async def _rc(db, q, **kw):
        called.append(kw)
        return {"trades_marked_for_close": 1}
    monkeypatch.setattr(cc, "request_close", _rc)
    leg = {"_id": "t", "close_requested": True, "netting_close_acked": True,
           "close_command": {"state": "requested", "reason": "friday_flat", "key": "k0",
                             "requested_at": (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()}}
    assert _run(cc.mandatory_close(None, leg, reason="friday_flat", actor="ff")) == "in_flight" and not called
    assert 'if trade.get("netting_close_acked"):' in inspect.getsource(cw.sweep)
    panic = open(os.path.join(BACKEND, "routes", "panic_routes.py")).read()
    assert '"netting_close_acked": {"$ne": True}' in panic


# ------------------------------------------------------------------------------------------------------ #52
def test_late_fill_intentional_and_hard_drawdown_are_mandatory_and_grace_blocks_expiry():
    import close_watchdog as cw
    for r in ("late_fill_after_expiry", "late_fill_after_lock", "late_fill_after_panic", "intentional_close",
              "hard_drawdown", "auto_deleverage_hard_drawdown", "unprotected_position"):
        assert cw.is_mandatory_close(r), r
    for r in ("auto_deleverage_var_cap", "auto_deleverage_sector_cap", "time_stop", "take_profit", "scalp_exit"):
        assert not cw.is_mandatory_close(r), r
    src = inspect.getsource(cw.sweep)
    i = src.index("if age >= CLOSE_REQUEST_TTL_SEC:")
    assert "if ack_in_grace(ack, now):" in src[i:i + 200] and "continue" in src[i:i + 300]   # #52 — no expiry < 120 s after a success ack


# ------------------------------------------------------------------------------------------------------ #53
def test_entry_costs_from_heartbeat_spread_and_in_deal_commission():
    from entry_costs import commission_update, spread_at_entry
    acc = {"current_spreads": {"GOLD#": 2.1, "EURUSD": 0.8}}
    assert spread_at_entry(acc, "XAUUSD") == 2.1 and spread_at_entry(acc, "GOLD#") == 2.1 and spread_at_entry(acc, "EURUSD") == 0.8
    assert spread_at_entry(acc, "GBPUSD") is None and spread_at_entry({}, "XAUUSD") is None
    u = commission_update("XAUUSD", 0.05, -0.35, account_type="standard", price=4000.0)   # $7/lot each way, $1 pip/lot
    assert u["commission_entry"] == -0.35 and u["commission_per_lot"] == 7.0 and u["commission_pips"] == 1.4
    u = commission_update("EURUSD", 0.10, -0.70)                                            # $7/lot, $10/pip → 1.4 pips round trip
    assert u["commission_pips"] == 1.4
    assert commission_update("EURUSD", 0.10, 0.0) == {} and commission_update("EURUSD", 0, -1.0) == {}


def test_breakeven_cost_buffer_uses_stored_costs_for_buy_and_sell():
    from trade_manager import scalp_breakeven_sl, scalp_cost_pips
    buy = {"symbol": "XAUUSD", "action": "BUY", "entry_price": 4000.0}
    sell = {"symbol": "XAUUSD", "action": "SELL", "entry_price": 4000.0}
    assert scalp_cost_pips(buy) == 2.5                                                      # default until the EA reports costs
    assert scalp_breakeven_sl(buy) == 4000.25 and scalp_breakeven_sl(sell) == 3999.75
    real_buy = {**buy, "spread_at_entry_pips": 1.2, "commission_pips": 1.4}
    real_sell = {**sell, "spread_at_entry_pips": 1.2, "commission_pips": 1.4}
    assert scalp_cost_pips(real_buy) == pytest.approx(2.6)
    assert scalp_breakeven_sl(real_buy) == 4000.26 and scalp_breakeven_sl(real_sell) == 3999.74


class _WS:
    def __init__(self):
        self.sent = []

    async def broadcast(self, uid, kind, payload):
        self.sent.append((kind, payload))


def _patch_tm(monkeypatch):
    import trade_manager as tm
    calls = []

    async def _stamp(db, filt, mod, extra=None):
        calls.append((filt, mod, extra))
        return {"intent_id": "i1", **mod}

    async def _noclamp(db, trade, new_sl, current, source):
        return None

    async def _no_db(*a, **k):
        raise AssertionError("unexpected DB write")
    monkeypatch.setattr(tm, "stamp_pending_modification", _stamp)
    monkeypatch.setattr(tm, "be_clamped_skip", _noclamp)
    monkeypatch.setattr(tm, "record_sl_loosen_refusal", _no_db)
    monkeypatch.setattr(tm, "ws_manager", _WS())
    return calls


def test_breakeven_trigger_r_sell_moves_stop_below_entry_by_real_costs(monkeypatch):
    import trade_manager as tm
    calls = _patch_tm(monkeypatch)
    sell = {"_id": "t1", "user_id": "u1", "account_id": "a1", "symbol": "XAUUSD", "action": "SELL", "entry_price": 4000.0,
            "stop_loss": 4005.0, "lot_size": 0.05, "sl_pips": 50, "tp_pips": [150, 150, 150], "scope": "breakout",
            "spread_at_entry_pips": 1.2, "commission_pips": 1.4}
    cfg = {"breakeven_trigger_r": 1.0}
    assert _run(tm._breakeven_trigger_r(None, dict(sell), cfg, 3996.0, 40.0)) is False and not calls     # +0.8R
    assert _run(tm._breakeven_trigger_r(None, dict(sell), cfg, 3995.0, 50.0)) is True                    # +1.0R
    filt, mod, extra = calls[0]
    assert mod["type"] == "MODIFY_SL" and mod["new_sl"] == 3999.74 and extra["be_cost_pips"] == 2.6     # entry − (1.2 + 1.4) pips
    # never loosens: a SELL stop already below entry−costs is left alone
    calls.clear()
    assert _run(tm._breakeven_trigger_r(None, {**sell, "stop_loss": 3999.0}, cfg, 3995.0, 50.0)) is False and not calls


def test_bridge_stamps_spread_and_commission_on_stoic_fills():
    src = open(os.path.join(BACKEND, "routes", "bridge_routes.py")).read()
    assert "from entry_costs import commission_update, spread_at_entry" in src
    assert 'update["spread_at_entry_pips"] = _sp' in src                                  # first OPEN report
    assert "async def _stamp_entry_costs" in src and src.count("await _stamp_entry_costs(db, existing, acc, payload)") == 2
    assert "**commission_update(payload.symbol, payload.lots, payload.commission," in src  # adopted sibling path


def test_optimizer_cannot_switch_breakeven_off_and_presets_never_do():
    import ai_optimizer as ao
    from strategy_presets import STRICTER_OR, safe_overlay
    assert "breakeven_enabled" not in ao.ALLOWED_FIELDS and "breakeven_trigger_r" in ao.ALLOWED_FIELDS
    assert ao.validate_recommendations([{"type": "config_change", "field": "breakeven_enabled", "to": False, "reason": "x"}],
                                       {"breakeven_enabled": True}) == []
    assert "breakeven_enabled" in STRICTER_OR
    assert safe_overlay({"breakeven_enabled": True}, {"breakeven_enabled": False, "breakeven_trigger_r": 0.4})["breakeven_enabled"] is True
    assert 'raise ValueError(f"{field} is not optimizer-tunable")' in inspect.getsource(ao.apply_recommendation)


# ------------------------------------------------------------------------------------------------------ #54
def test_sector_cap_advisory_pulse_only_on_a_new_alert(monkeypatch):
    from portfolio import auto_deleverage as ad
    pulses, alerts = [], []

    class _Alerting:
        @staticmethod
        async def raise_alert(db, kind, sev, msg, dedup_key=None, meta=None):
            alerts.append(dedup_key)
            return None if len(alerts) > 1 else "new-id"                                 # 2nd call = deduped

    class _Runner:
        @staticmethod
        async def _record_pulse(db, cfg, **kw):
            pulses.append(kw)

    class _Cfgs:
        async def find_one(self, q):
            return {"user_id": "u", "account_id": "a"}

    class _Db:
        bot_configs = _Cfgs()
    monkeypatch.setitem(sys.modules, "alerting", _Alerting)
    monkeypatch.setitem(sys.modules, "bot_runner", _Runner)
    acc = {"_id": "a", "user_id": "u", "label": "L"}
    snap = {"advisory_triggers": ["sector_cap:fx_major"], "advisory_actions": [{"symbol": "EURUSD", "lot_size": 0.1, "reason": "auto_deleverage_sector_cap"}]}
    _run(ad._advise_sector_caps(_Db(), acc, snap))
    _run(ad._advise_sector_caps(_Db(), acc, snap))
    _run(ad._advise_sector_caps(_Db(), acc, snap))
    assert len(alerts) == 3 and len(pulses) == 1
