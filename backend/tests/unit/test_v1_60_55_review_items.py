"""v1.60.55 — review items #44 (mandatory closes never expire, re-dispatch with backoff; discretionary expire only with
the EA online), #45 (sector-cap / corr deleverage advisory behind SECTOR_CAP_DELEVERAGE_ENABLED), #46 (shared BE
default — see test_v1_60_54_var_deleverage_breakeven), #47 (scalp base_symbol cap, _skip_pulse_at prune, legacy
live_at), #48 (friday_flat / eod / panic take over unacked closes)."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NOW = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------------------------------------------ #44
def test_mandatory_vs_discretionary_close_reasons():
    from close_watchdog import is_mandatory_close
    for r in ("panic", "friday_flat", "eod_flatten", "pre_news_protect", "emergency_unprotected", "emergency_specs_stale",
              "manual", "manual_telegram", "nl_command", "excess_over_cap", "panic:admin"):
        assert is_mandatory_close(r), r
    for r in ("auto_deleverage_var_cap", "auto_deleverage_sector_cap_commodity", "auto_deleverage_hard_drawdown",
              "time_stop", "take_profit", "scalp_time_stop", "queued_timeout", None, ""):
        assert not is_mandatory_close(r), r


def test_redispatch_backoff_doubles_and_caps():
    from close_watchdog import redispatch_due
    assert redispatch_due(0, 59) is False and redispatch_due(0, 60) is True
    assert redispatch_due(1, 119) is False and redispatch_due(1, 120) is True
    assert redispatch_due(2, 240) is True and redispatch_due(3, 479) is False
    assert redispatch_due(10, 899) is False and redispatch_due(10, 900) is True      # capped at 15 min


def test_ea_online_is_a_fresh_heartbeat():
    from close_watchdog import ea_online
    assert ea_online({"last_heartbeat": (NOW - timedelta(seconds=30)).isoformat()}, NOW) is True
    assert ea_online({"last_heartbeat": (NOW - timedelta(seconds=120)).isoformat()}, NOW) is False
    assert ea_online({}, NOW) is False and ea_online(None, NOW) is False


def test_watchdog_source_never_expires_mandatory_and_gates_expiry_on_ea_online():
    src = open(os.path.join(BACKEND, "close_watchdog.py")).read()
    i = src.index("if mandatory:")
    assert "_redispatch(db, trade, row, now)" in src[i:i + 400] and "continue" in src[i:i + 400]
    j = src.index("if age >= CLOSE_REQUEST_TTL_SEC:")
    assert "if not ea_online(acc, now):" in src[j:j + 400] and "kept_offline" in src[j:j + 600]
    assert j > i                                   # expiry branch is only reachable for discretionary reasons


# ------------------------------------------------------------------------------------------------------ #45
def _pos(tid, symbol="EURUSD", lot=1.0, entry=1.1, sl=1.09, action="BUY", pnl=None):
    p = {"_id": tid, "symbol": symbol, "action": action, "lot_size": lot, "entry_price": entry, "stop_loss": sl, "status": "open"}
    if pnl is not None:
        p["live_pnl"] = pnl
    return p


async def _none(_sym):
    return None


def _snapshot(monkeypatch, positions, equity=10_000.0):
    import portfolio.var as pv
    import portfolio.risk_manager as rm
    monkeypatch.setattr(pv, "_atr_pct", _none)

    async def _dd(*a, **k):
        return {"hard_breach": False, "soft_breach": False, "dd_pct": 0.0}
    monkeypatch.setattr(rm, "get_drawdown", _dd)
    account = {"_id": "acc1", "user_id": "u1", "equity": equity, "balance": equity}
    return _run(rm.build_snapshot(None, account=account, open_positions=positions, cfg={"risk_level": "low"}))


def test_sector_cap_breach_is_advisory_by_default(monkeypatch):
    """Four 1-lot EURUSD ($110k each = 4,400 % of $10k equity) blow the 600 % fx_major cap — but with the flag OFF
    nothing is closed: the breach lands in advisory_actions, needs_deleveraging stays False."""
    monkeypatch.delenv("SECTOR_CAP_DELEVERAGE_ENABLED", raising=False)
    # wide stops so the stop-bounded-book exemption does NOT apply (each leg risks $1,000 → ×1.5 → 60 % of equity)
    pos = [_pos(f"t{i}", sl=1.09, pnl=-50.0 * i) for i in range(4)]
    snap = _snapshot(monkeypatch, pos)
    assert snap["sectors"]["fx_major"]["over_cap"] is True
    assert snap["sector_cap_deleverage_enabled"] is False
    assert not [a for a in snap["actions"] if "sector_cap" in a["reason"] or "combined_corr" in a["reason"]]
    assert snap["advisory_triggers"] == ["sector_cap_fx_major"] or "sector_cap_fx_major" in snap["advisory_triggers"]
    assert snap["advisory_actions"][0]["reason"] == "auto_deleverage_sector_cap_fx_major"
    assert snap["advisory_actions"][0]["trade_id"] == "t3"           # worst loser
    # the only executable action left is the (legitimate) var_cap one — 4 × $1,000 open risk on $10k is 60 % > 5 % cap
    assert all(a["reason"] == "auto_deleverage_var_cap" for a in snap["actions"])


def test_sector_cap_enabled_by_flag_closes_worst_loser(monkeypatch):
    monkeypatch.setenv("SECTOR_CAP_DELEVERAGE_ENABLED", "true")
    pos = [_pos(f"t{i}", sl=1.09, pnl=-50.0 * i) for i in range(4)]
    snap = _snapshot(monkeypatch, pos)
    assert snap["sector_cap_deleverage_enabled"] is True and snap["advisory_actions"] == []
    assert any(a["reason"] == "auto_deleverage_sector_cap_fx_major" and a["trade_id"] == "t3" for a in snap["actions"])


def test_sector_cap_honours_var_cap_exemptions(monkeypatch):
    """Same exemptions as var_cap: a stop-bounded book inside the VaR cap, or only zero-risk (BE) positions, is
    never a sector-cap cull — not even advisory."""
    monkeypatch.setenv("SECTOR_CAP_DELEVERAGE_ENABLED", "true")
    tight = [_pos(f"t{i}", sl=1.0995) for i in range(4)]              # $50 risk each → 300 × 1.5 / 10k = 3 % < 5 %
    snap = _snapshot(monkeypatch, tight)
    assert snap["sectors"]["fx_major"]["over_cap"] is True
    assert not [a for a in snap["actions"] + snap["advisory_actions"] if "sector_cap" in a["reason"]]
    assert any("sector_cap_fx_major cull skipped" in n for n in snap["var"]["notes"])
    be = [_pos(f"t{i}", sl=1.1005) for i in range(4)]                # stops above entry → zero open risk
    snap = _snapshot(monkeypatch, be)
    assert not [a for a in snap["actions"] + snap["advisory_actions"] if "sector_cap" in a["reason"]]


def test_sector_cap_advisory_alert_is_wired_and_in_release_notes():
    src = open(os.path.join(BACKEND, "portfolio", "auto_deleverage.py")).read()
    assert 'raise_alert(db, "sector_cap_advisory", "medium"' in src and "_advise_sector_caps(db, acc, snap)" in src
    notes = open(os.path.join(os.path.dirname(BACKEND), "docs", "RELEASE_NOTES_v1.60.55.md")).read()
    assert "SECTOR_CAP_DELEVERAGE_ENABLED" in notes and "FINNHUB_API_KEY" in notes and "macro_freeze_check" in notes


# ------------------------------------------------------------------------------------------------------ #47
def test_scalp_direction_cap_uses_base_symbol():
    src = open(os.path.join(BACKEND, "scalp", "engine.py")).read()
    assert "base_symbol=_base_symbol(self.symbol) or self.symbol" in src


def test_skip_pulse_cache_is_pruned():
    import time
    import trade_manager as tm
    tm._skip_pulse_at.clear()
    old = time.time() - 7200
    for i in range(600):
        tm._skip_pulse_at[f"old{i}"] = old

    class _DB:
        pass
    _run(tm._management_skipped_pulse(_DB(), {"_id": "fresh", "symbol": "XAUUSD"}, {"_id": None}, {"age_ms": 99999}))
    assert len(tm._skip_pulse_at) < 10 and "fresh" in tm._skip_pulse_at
    tm._skip_pulse_at.clear()


def test_legacy_live_at_is_fresh_only_for_ea_1_66():
    from broker_price import from_report, ea_at_least
    at = NOW.isoformat()
    legacy = {"live_price": 4000.0, "live_at": at}
    now_ms = NOW.timestamp() * 1000
    assert from_report(legacy, now_ms, ea_version="1.65") is None
    assert from_report(legacy, now_ms, ea_version=None) is None
    assert from_report(legacy, now_ms, ea_version="1.66")["age_ms"] == 0
    assert from_report({"live_price": 4000.0, "live_price_at": at}, now_ms, ea_version="1.40")["age_ms"] == 0
    assert ea_at_least("1.66.2", 1, 66) and not ea_at_least("garbage", 1, 66)


# ------------------------------------------------------------------------------------------------------ #48
def test_close_in_flight_requires_a_successful_terminal_ack():
    from close_commands import close_in_flight
    base = {"close_requested": True, "close_command": {"state": "requested", "reason": "auto_deleverage_var_cap"}}
    assert close_in_flight(base) is False                                              # unacked → takeover allowed
    assert close_in_flight({**base, "close_command": {**base["close_command"], "terminal_ack": {"success": False}}}) is False
    assert close_in_flight({**base, "close_command": {**base["close_command"], "terminal_ack": {"success": True}}}) is True
    assert close_in_flight({"close_requested": False}) is False


def test_mandatory_closers_no_longer_skip_close_requested_trades():
    ff = open(os.path.join(BACKEND, "friday_flat.py")).read()
    assert '"close_requested": {"$ne": True}' not in ff and '"friday_flat_at": {"$exists": False}' not in ff
    assert 'mandatory_close(db, tr, reason="friday_flat"' in ff
    eod = open(os.path.join(BACKEND, "eod_flatten.py")).read()
    assert 'mandatory_close(db, tr, reason="eod_flatten"' in eod and 'if tr.get("pending_modification"):\n                continue' not in eod
    panic = open(os.path.join(BACKEND, "routes", "panic_routes.py")).read()
    assert 'pending_modification={"type": "FULL_CLOSE", "reason": "panic"}' in panic and "_ea_supports_full_close" in panic
    rm = open(os.path.join(BACKEND, "portfolio", "risk_manager.py")).read()
    assert 'pending_modification={"type": "FULL_CLOSE", "reason": reason}, emergency=True' in rm   # v1.60.54 confirmed
