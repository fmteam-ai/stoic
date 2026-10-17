"""#80 — tp1_bank_runner end-to-end against a simulated broker: entry sends SL + TP2 to the broker (TP1 is bot-side),
price reaches TP1 → PARTIAL_CLOSE → EA ack → follow-up MODIFY_SL carries SL = entry ± $1 AND TP = TP2 → runner trails.
Control: fixed_tp still sends TP1 at entry."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

from bson import ObjectId

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

import exit_policy_tp1 as X  # noqa: E402


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


class Broker:
    """Minimal MT5-like position: holds SL/TP/volume, closes when the broker TP/SL is touched, acks commands."""

    def __init__(self, action, entry, sl, tp, volume):
        self.action, self.price, self.sl, self.tp, self.volume = action, entry, sl, tp, volume
        self.closed = None

    def tick(self, price):
        self.price = price
        if self.closed:
            return
        if (self.action == "BUY" and price >= self.tp) or (self.action == "SELL" and price <= self.tp):
            self.closed = ("tp", self.tp)
        elif (self.action == "BUY" and price <= self.sl) or (self.action == "SELL" and price >= self.sl):
            self.closed = ("sl", self.sl)

    def apply(self, pm):
        if pm["type"] == "PARTIAL_CLOSE":
            self.volume = pm["new_volume"]                       # EA 1.65 ignores new_sl/new_tp on PARTIAL_CLOSE
        elif pm["type"] == "MODIFY_SL":
            self.sl = pm["new_sl"]
            if pm.get("new_tp") is not None:
                self.tp = pm["new_tp"]


def _entry_signal_through_runner_shape(cfg, signal):
    """Mirror of the bot_runner #80 block: what the broker order carries at entry."""
    from strategy_engines import engine_for_signal
    plan = None
    if X.policy_for(cfg) == "tp1_bank_runner" and engine_for_signal(signal) in X.ENGINES:
        plan = X.plan_targets(signal["entry_price"], signal["stop_loss"], signal["take_profit"], signal["action"], cfg.get("tp2_multiple") or 2.0)
    return {"take_profit": plan["tp2"] if plan else signal["take_profit"], "tp_plan": plan,
            "exit_policy": "tp1_bank_runner" if plan else "fixed_tp", "original_take_profit": signal["take_profit"]}


def test_80_fixed_tp_sends_tp1_at_entry():
    sig = {"symbol": "XAUUSD", "action": "BUY", "entry_price": 4100.0, "stop_loss": 4096.0, "take_profit": 4104.0, "engine": "hf_scalp"}
    out = _entry_signal_through_runner_shape({"exit_policy": "fixed_tp"}, sig)
    assert out == {"take_profit": 4104.0, "tp_plan": None, "exit_policy": "fixed_tp", "original_take_profit": 4104.0}
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "bot_runner.py")).read()
    assert '"take_profit": _tp_plan["tp2"] if _tp_plan else signal["take_profit"]' in src


def test_80_full_sequence_with_simulated_broker(monkeypatch):
    import trade_manager as TM
    import routes.bridge_routes as br
    cfg = {"exit_policy": "tp1_bank_runner", "tp1_close_fraction": 0.5, "be_plus_offset_price": 1.0, "tp2_multiple": 2.0}
    sig = {"symbol": "XAUUSD", "action": "BUY", "entry_price": 4100.0, "stop_loss": 4096.0, "take_profit": 4104.0, "engine": "hf_scalp"}
    entry = _entry_signal_through_runner_shape(cfg, sig)
    assert entry["take_profit"] == 4108.0 and entry["tp_plan"]["tp1"] == 4104.0        # broker TP = TP2 at entry

    db = FakeDb()
    acc_id, tid = ObjectId(), ObjectId()
    acc = {"_id": acc_id, "user_id": "u", "ea_version": "1.65"}
    db.accounts.rows.append(acc)
    trade = {"_id": tid, "user_id": "u", "account_id": str(acc_id), "symbol": "XAUUSD", "action": "BUY", "entry_price": 4100.0,
             "stop_loss": 4096.0, "original_stop_loss": 4096.0, "take_profit": entry["take_profit"], "original_take_profit": 4104.0,
             "tp_plan": entry["tp_plan"], "exit_policy": "tp1_bank_runner", "lot_size": 0.2, "original_lot_size": 0.2,
             "status": "open", "scope": "hf_scalp", "engine": "hf_scalp", "mt5_ticket": 77, "origin": "auto"}
    db.trades.rows.append(trade)
    broker = Broker("BUY", 4100.0, 4096.0, entry["take_profit"], 0.2)

    async def stamp(db_, flt, pm, extra=None):
        row = db.trades.rows[0]
        row["pending_modification"] = dict(pm)
        row.update(extra or {})
        return {"ok": True}

    find_acc = AsyncMock(return_value=(acc, None))
    with patch("protection_guard.find_account", find_acc), patch("protection_guard.broker_stop_constraints", lambda a, s: {"min_stop_distance_px": 0.3}), \
            patch.object(TM, "stamp_pending_modification", stamp), patch.object(TM.ws_manager, "broadcast", AsyncMock()):
        # 1) price below TP1 → nothing, broker still open with TP2
        broker.tick(4103.5)
        assert _run(TM._tp1_bank_runner_exits(db, dict(db.trades.rows[0]), cfg, 4103.5)) is False and broker.closed is None
        # 2) price reaches TP1 → broker does NOT close (its TP is TP2) → bot banks
        broker.tick(4104.0)
        assert broker.closed is None, "broker TP must be TP2, otherwise 100% closes before the bank"
        assert _run(TM._tp1_bank_runner_exits(db, dict(db.trades.rows[0]), cfg, 4104.0)) is True
    pm = db.trades.rows[0]["pending_modification"]
    assert pm == {"type": "PARTIAL_CLOSE", "new_volume": 0.1, "new_sl": 4101.0, "new_tp": 4108.0, "reason": "tp1_bank"}
    broker.apply(pm)                                                     # EA closes 0.1, ignores new_sl/new_tp
    assert broker.volume == 0.1 and broker.sl == 4096.0 and broker.tp == 4108.0

    # 3) EA acks the partial → bridge enqueues the follow-up MODIFY_SL carrying SL AND TP2
    ack = br.BridgeModificationAck(bridge_token="t", trade_id=str(tid), type="PARTIAL_CLOSE", success=True, new_volume=0.1,
                                   remaining_volume=0.1, intent_id=pm.get("intent_id"))
    with patch.object(br, "get_db", lambda: db), patch.object(br, "_account_by_token", AsyncMock(return_value=acc)), \
            patch("command_fence.is_stale_ack", lambda *a, **k: False), patch("command_fence.ack_type_mismatch", lambda *a, **k: False), \
            patch("command_fence.is_close_leg_ack", lambda *a, **k: False), patch("command_fence.is_duplicate_close_leg_ack", lambda *a, **k: False), \
            patch("command_fence.ack_clears_pending", lambda *a, **k: True):
        _run(br.modification_ack(ack))
    row = db.trades.rows[0]
    follow = row["pending_modification"]
    assert follow["type"] == "MODIFY_SL" and follow["reason"] == "tier1_combo_followup"
    assert follow["new_sl"] == 4101.0 and follow["new_tp"] == 4108.0, follow
    assert row["lot_size"] == 0.1 and row["tp1_closed"] is True
    broker.apply(follow)
    assert broker.sl == 4101.0 and broker.tp == 4108.0

    # 4) follow-up acked → runner trails behind the M5 swing, profit direction only
    row["pending_modification"] = None
    row["stop_loss"] = 4101.0
    row["be_target_sl"] = 4101.0
    bars = [{"t": i, "o": 0, "h": 4105 + i * 0.5, "l": 4103 + i * 0.5, "c": 4104 + i * 0.5} for i in range(16)]
    db.intraday_candles.rows.append({"user_id": "u", "symbol": "XAUUSD", "timeframe": "M5", "bars": bars})
    with patch("protection_guard.find_account", find_acc), patch("protection_guard.broker_stop_constraints", lambda a, s: {"min_stop_distance_px": 0.3}), \
            patch.object(TM, "stamp_pending_modification", stamp), patch.object(TM.ws_manager, "broadcast", AsyncMock()):
        broker.tick(4110.0)
        assert _run(TM._tp1_bank_runner_exits(db, dict(row), cfg, 4110.0)) is True
    trail = db.trades.rows[0]["pending_modification"]
    assert trail["type"] == "MODIFY_SL" and trail["reason"] == "tp1_runner_trail" and 4101.0 < trail["new_sl"] < 4110.0
    broker.apply(trail)
    # a later, lower swing must never loosen the stop
    row = db.trades.rows[0]
    row["pending_modification"] = None
    row["stop_loss"] = trail["new_sl"]
    row["be_target_sl"] = trail["new_sl"]
    db.intraday_candles.rows[0]["bars"] = [{"t": i, "o": 0, "h": 4102, "l": 4100, "c": 4101} for i in range(16)]
    with patch("protection_guard.find_account", find_acc), patch("protection_guard.broker_stop_constraints", lambda a, s: {}), \
            patch.object(TM, "stamp_pending_modification", stamp), patch.object(TM.ws_manager, "broadcast", AsyncMock()):
        assert _run(TM._tp1_bank_runner_exits(db, dict(row), cfg, 4109.0)) is True
    assert db.trades.rows[0]["pending_modification"] is None and broker.sl == trail["new_sl"]
    # 5) TP2 closes the runner at the broker
    broker.tick(4108.0)
    assert broker.closed == ("tp", 4108.0)


# ── #75 / #76 / #77 ─────────────────────────────────────────────────────────────────────────────────

def test_75_trend_day_pullback_shadow_default_and_rule():
    from payoff_guard import trend_day_pullback_check as chk
    from models import BotConfigUpdate
    assert BotConfigUpdate().trend_day_pullback_enabled is False
    ride = {"direction": "UP", "day_range_pct": 1.4, "range_pos_pct": 70}
    ext = {"last_price": 4120.0, "ema20": 4110.0, "session_vwap": 4108.0, "atr15": 4.0}          # 2.5×ATR above the mean
    r = chk("BUY", ext, ride)
    assert r["ok"] is False and "wait for the pullback" in r["why"] and r["dist_ema20_atr"] == 2.5
    back = {"last_price": 4111.0, "ema20": 4110.0, "session_vwap": 4105.0, "atr15": 4.0}         # 0.25×ATR from EMA20
    assert chk("BUY", back, ride)["ok"] is True
    assert chk("SELL", {"last_price": 4100.0, "ema20": 4110.0, "session_vwap": 4108.0, "atr15": 4.0}, ride)["ok"] is False  # extended below
    assert chk("BUY", ext, None) is None                                                          # not a trend day → no verdict
    assert chk("BUY", {"ema20": 4110.0}, ride)["ok"] is False                                      # unavailable → not ok (shadow records it)
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "bot_runner.py")).read()
    assert 'if not _pb["ok"] and cfg.get("trend_day_pullback_enabled"):' in src and '"trend_day_pullback"' in src


def test_76_news_veto_inactive_when_cache_older_than_6h():
    import time
    from news_understanding import news_cache_stale
    from ai_signals import news_veto_state
    now = time.time()
    from datetime import datetime, timezone
    fresh = datetime.fromtimestamp(now - 3600, tz=timezone.utc).isoformat()
    old = datetime.fromtimestamp(now - 7 * 3600, tz=timezone.utc).isoformat()
    assert news_cache_stale(fresh, now) == (False, 1.0) and news_cache_stale(old, now)[0] is True and news_cache_stale(None) == (True, None)
    assert news_veto_state({"available": True, "score": -0.5, "fetched_at": fresh}, True)["active"] is True
    st = news_veto_state({"available": True, "score": -0.5, "fetched_at": old}, True)
    assert st["active"] is False and st["note"] == "news veto inactive — news cache stale (> 6 h)"
    diag = open(os.path.join(os.path.dirname(__file__), "..", "..", "routes", "diagnostic_routes.py")).read()
    assert "news cache stale" in diag and "news_cache_stale(ns.get(\"last_scored_at\"))" in diag


def test_77_anti_tilt_banner_states_the_real_loss_rule():
    from anti_tilt import rule_text, classify
    st = {"enabled": True, "n": 3, "hours": 4}
    txt = rule_text(st)
    assert "REAL loss" in txt and "scratches never count" in txt and "engine_live_features_enabled is ON" in txt and "newest 3" in txt
    out = classify([], {"anti_tilt_consecutive_losses": 3, "anti_tilt_freeze_hours": 4}, None, None)
    assert out["rule"] == txt and out["active"] is False
    diag = open(os.path.join(os.path.dirname(__file__), "..", "..", "routes", "diagnostic_routes.py")).read()
    assert "real losses" in diag and 'rule: {rule}' in diag
