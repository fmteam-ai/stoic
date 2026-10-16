"""v1.60.60 — #71 Gold Daily Bias (weights, missing inputs, flag defaults, gates, post-news rule, surprise) and
#72 TP1 bank + protected runner (BUY/SELL, lot rounding, min stop, partial-failure full close, trail never loosens, report split)."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

import exit_policy_tp1 as X  # noqa: E402
import gold_bias as GB  # noqa: E402


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


# ── #71 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_71_weights_fixed_and_sum_to_100():
    assert sum(GB.WEIGHTS.values()) == 100
    assert GB.WEIGHTS == {"d1_h4_trend": 25, "dxy": 15, "real_yields": 15, "news_sentiment": 10, "fed_tone": 10,
                          "risk_regime": 10, "cot": 5, "calendar_surprise": 10}
    full_long = GB.compute_bias({k: 1.0 for k in GB.WEIGHTS})
    assert full_long["score"] == 100 and full_long["bias"] == "LONG" and full_long["strong"] and full_long["unavailable"] == []
    assert GB.compute_bias({k: -1.0 for k in GB.WEIGHTS})["bias"] == "SHORT"
    mixed = GB.compute_bias({"d1_h4_trend": 1.0, "dxy": -1.0, "real_yields": 0.0})
    assert mixed["score"] == 10.0 and mixed["bias"] == "NEUTRAL"


def test_71_missing_inputs_count_zero_and_never_fail_open():
    empty = GB.compute_bias({})
    assert empty["score"] == 0 and empty["bias"] == "NEUTRAL" and set(empty["unavailable"]) == set(GB.WEIGHTS)
    assert any(r.startswith("unavailable:") for r in empty["reasons"])
    one = GB.compute_bias({"d1_h4_trend": 1.0, "d1_h4_trend_why": "D1 up · H4 up"})
    assert one["score"] == 25 and one["bias"] == "LONG" and "d1_h4_trend +25 — D1 up · H4 up" in one["reasons"]
    assert len(one["unavailable"]) == 7
    assert GB.compute_bias({"dxy": 5.0})["score"] == 15        # inputs are clamped to [-1, 1]


def test_71_flag_defaults_off_and_gates_inert():
    from models import BotConfigUpdate
    cfg = BotConfigUpdate()
    assert cfg.gold_bias_gtp_direction_enabled is False and cfg.gold_bias_block_counter_enabled is False
    strong_long = GB.compute_bias({k: 1.0 for k in GB.WEIGHTS})
    assert GB.gtp_direction_allowed(strong_long, "SELL", {}) == (True, "")
    assert GB.counter_bias_block(strong_long, "SELL", "hf_scalp", {}) is None
    # ON
    on = {"gold_bias_gtp_direction_enabled": True, "gold_bias_block_counter_enabled": True}
    assert GB.gtp_direction_allowed(strong_long, "BUY", on)[0] is True
    ok, why = GB.gtp_direction_allowed(strong_long, "SELL", on)
    assert ok is False and "not in bias direction" in why
    assert GB.gtp_direction_allowed(GB.compute_bias({}), "BUY", on)[0] is False
    assert "counter-bias" in GB.counter_bias_block(strong_long, "SELL", "hf_scalp_fast", on)
    assert GB.counter_bias_block(strong_long, "BUY", "breakout_m15", on) is None
    weak = GB.compute_bias({"d1_h4_trend": 1.0, "dxy": 1.0})          # +40, not strong
    assert GB.counter_bias_block(weak, "SELL", "hf_scalp", on) is None
    assert GB.counter_bias_block(strong_long, "SELL", "range_fade", on) is None   # not in scope


def test_71_surprise_and_post_news_rule():
    assert GB.surprise(3.4, 3.2) == pytest.approx(0.2) and GB.surprise(None, 3.2) is None
    v, why = GB.surprise_value("CPI m/m", 0.4, 0.2)
    assert v == -1.0 and "surprise +0.20" in why                      # hot CPI → gold SHORT
    assert GB.surprise_value("Non-Farm Employment Change", 150, 200)[0] == 1.0
    assert GB.surprise_value("CPI y/y", None, 3.2) == (None, "CPI y/y: actual missing")
    kw = dict(freeze_active=False, spread_ok=True, m5_close=4105.0, first5_high=4100.0, first5_low=4090.0)
    assert GB.post_news_entry_ok(bias="LONG", **kw)[0] is True
    assert GB.post_news_entry_ok(bias="SHORT", **kw)[0] is False
    assert GB.post_news_entry_ok(**{**kw, "freeze_active": True}, bias="LONG") == (False, "news freeze still active")
    assert GB.post_news_entry_ok(**{**kw, "spread_ok": False}, bias="LONG")[0] is False
    assert GB.post_news_entry_ok(**{**kw, "m5_close": None}, bias="LONG")[0] is False
    assert GB.alignment({"bias": "LONG"}, "BUY") == "with" and GB.alignment({"bias": "LONG"}, "SELL") == "against"
    assert GB.alignment({"bias": "NEUTRAL"}, "BUY") == "n/a"


def test_71_shadow_log_and_outcome_resolution():
    db = FakeDb()
    bias = GB.compute_bias({"d1_h4_trend": 1.0})
    _run(GB.shadow_log(db, {"_id": "s1", "user_id": "u", "symbol": "XAUUSD", "action": "SELL", "scope": "hf_scalp"}, bias))
    _run(GB.shadow_log(db, {"_id": "s2", "user_id": "u", "symbol": "EURUSD", "action": "BUY"}, bias))   # not gold → ignored
    assert len(db.gold_bias_shadow.rows) == 1 and db.gold_bias_shadow.rows[0]["alignment"] == "against"
    db.trades.rows.append({"signal_id": "s1", "status": "closed", "pnl": -12.5})
    assert _run(GB.resolve_shadow_outcomes(db)) == 1
    assert db.gold_bias_shadow.rows[0]["outcome"] == "loss"
    summ = _run(GB.shadow_summary(db, "u"))
    assert summ["against"] == {"n": 1, "wins": 0, "pnl": -12.5}


def test_71_calendar_outcomes_parse_actual_or_mark_missing():
    from economic_calendar import record_calendar_outcomes
    db = FakeDb()
    past = 1_000.0
    events = [{"title": "CPI m/m", "country": "USD", "impact": "high", "when_ts": past, "when": "x", "forecast": "0.2%", "previous": "0.3%", "actual": "0.4%"},
              {"title": "Non-Farm Employment Change", "country": "USD", "impact": "high", "when_ts": past, "when": "x", "forecast": "200K", "previous": "180K", "actual": None},
              {"title": "CPI m/m", "country": "EUR", "impact": "high", "when_ts": past, "when": "x", "forecast": "0.2", "actual": "0.4"}]
    assert _run(record_calendar_outcomes(db, events)) == 2
    rows = {r["event"]: r for r in db.calendar_outcomes.rows}
    assert rows["CPI m/m"]["surprise"] == pytest.approx(0.2) and rows["CPI m/m"]["surprise_value"] == -1.0
    assert rows["Non-Farm Employment Change"]["actual_missing"] is True and rows["Non-Farm Employment Change"]["surprise"] is None


# ── #72 ──────────────────────────────────────────────────────────────────────────────────────────────

def test_72_defaults_and_targets_buy_sell():
    from models import BotConfigUpdate
    c = BotConfigUpdate()
    assert (c.exit_policy, c.tp1_close_fraction, c.be_plus_offset_price, c.tp2_multiple) == ("fixed_tp", 0.5, 1.0, 2.0)
    assert X.policy_for({}) == "fixed_tp" and X.policy_for({"exit_policy": "bogus"}) == "fixed_tp"
    assert not X.applies({"scope": "hf_scalp", "status": "open"}, {})
    assert X.applies({"scope": "gold_trend_pulse", "status": "open"}, {"exit_policy": "tp1_bank_runner"})
    assert not X.applies({"scope": "range_fade", "status": "open"}, {"exit_policy": "tp1_bank_runner"})
    b = X.plan_targets(4100.0, 4096.0, 4104.0, "BUY")
    assert (b["tp1"], b["tp2"], b["tp1_distance"]) == (4104.0, 4108.0, 4.0)
    s = X.plan_targets(4100.0, 4104.0, 4097.0, "SELL", 2.0)
    assert s["tp2"] == 4094.0
    assert X.protected_sl(4100.0, "BUY") == 4101.0 and X.protected_sl(4100.0, "SELL") == 4099.0
    assert X.tp1_reached("BUY", 4104.0, 4104.0) and not X.tp1_reached("BUY", 4103.9, 4104.0)
    assert X.tp1_reached("SELL", 4097.0, 4097.0) and X.tp1_reached("SELL", 4097.05, 4097.0, buffer=0.1)


@pytest.mark.parametrize("lot,step,min_lot,expect_close,expect_keep,sl_only", [
    (0.01, 0.01, 0.01, 0.0, 0.01, True),        # cannot split → SL only
    (0.15, 0.01, 0.01, 0.07, 0.08, False),      # floor 0.075 → 0.07
    (0.2, 0.01, 0.01, 0.1, 0.1, False),
    (0.2, 0.1, 0.1, 0.1, 0.1, False),
    (0.3, 0.1, 0.1, 0.1, 0.2, False),           # 0.15 floored to the 0.1 step
])
def test_72_lot_rounding(lot, step, min_lot, expect_close, expect_keep, sl_only):
    tr = {"action": "BUY", "entry_price": 4100.0, "lot_size": lot, "stop_loss": 4096.0}
    bp = X.bank_plan(tr, 4104.0, lot_step=step, min_lot=min_lot)
    assert bp["close_volume"] == pytest.approx(expect_close) and bp["keep_volume"] == pytest.approx(expect_keep)
    assert bp["sl_only"] is sl_only and bp["new_sl"] == 4101.0


def test_72_min_stop_distance_and_never_loosen():
    tr = {"action": "BUY", "entry_price": 4100.0, "lot_size": 0.2, "stop_loss": 4096.0}
    bp = X.bank_plan(tr, 4101.5, lot_step=0.01, min_lot=0.01, min_stop_distance_px=1.0)   # entry+1 = 4101 is only 0.5 below price
    assert bp["new_sl"] == 4100.5 and bp["new_sl"] > 4096.0
    sell = {"action": "SELL", "entry_price": 4100.0, "lot_size": 0.2, "stop_loss": 4104.0}
    bs = X.bank_plan(sell, 4097.0, min_stop_distance_px=0.5)
    assert bs["new_sl"] == 4099.0
    already = {"action": "BUY", "entry_price": 4100.0, "lot_size": 0.2, "stop_loss": 4102.0}    # stop already tighter than entry+1
    assert X.bank_plan(already, 4104.0)["new_sl"] == 4102.0


def test_72_trailing_stop_never_loosens_and_picks_tighter():
    bars = [{"high": 4105 + i * 0.5, "low": 4103 + i * 0.5, "close": 4104 + i * 0.5} for i in range(16)]
    # BUY: swing low of last 6 = 4103+10*0.5 = 4108.0 ; ATR ≈ 2 → price-ATR = 4110-2 = 4108 → tighter = max = 4108
    assert X.trail_sl("BUY", 4101.0, 4110.0, bars) == 4108.0
    assert X.trail_sl("BUY", 4109.0, 4110.0, bars) is None                 # would loosen
    assert X.trail_sl("BUY", 4101.0, 4110.0, bars, min_stop_distance_px=5.0) is None   # min stop violated → no move
    sbars = [{"high": 4105 - i * 0.5, "low": 4103 - i * 0.5, "close": 4104 - i * 0.5} for i in range(16)]
    new = X.trail_sl("SELL", 4110.0, 4096.0, sbars)
    assert new is not None and new < 4110.0 and X.trail_sl("SELL", new, 4096.0, sbars) is None
    assert X.trail_sl("BUY", 4101.0, 4110.0, []) is None


def test_72_partial_failure_leads_to_full_close(monkeypatch):
    import trade_manager as TM
    db = FakeDb()
    tr = {"_id": "t1", "user_id": "u", "account_id": "a", "symbol": "XAUUSD", "action": "BUY", "entry_price": 4100.0, "stop_loss": 4096.0,
          "take_profit": 4104.0, "lot_size": 0.2, "status": "open", "scope": "hf_scalp", "mt5_ticket": 1,
          "tp_plan": X.plan_targets(4100.0, 4096.0, 4104.0, "BUY"), "tp1_bank_attempts": 1,
          "pending_modification": {"type": "PARTIAL_CLOSE", "last_error": "10013 invalid volume"}}
    db.trades.rows.append(dict(tr))
    closed = AsyncMock(return_value="issued")
    cfg = {"exit_policy": "tp1_bank_runner"}
    with patch("protection_guard.find_account", AsyncMock(return_value=({"_id": "a"}, None))), \
            patch("protection_guard.broker_stop_constraints", lambda acc, sym: {}), \
            patch("close_commands.mandatory_close", closed), \
            patch.object(TM, "stamp_pending_modification", AsyncMock(return_value={"ok": True})):
        assert _run(TM._tp1_bank_runner_exits(db, dict(tr), cfg, 4104.2)) is True
    assert closed.await_count == 1 and closed.await_args.kwargs["reason"] == "tp1_bank_failed"
    assert db.trades.rows[0].get("tp1_bank_failed") is True


def test_72_bank_sends_partial_plus_sl_and_tp2_then_trails(monkeypatch):
    import trade_manager as TM
    db = FakeDb()
    tr = {"_id": "t2", "user_id": "u", "account_id": "a", "symbol": "XAUUSD", "action": "SELL", "entry_price": 4100.0, "stop_loss": 4104.0,
          "take_profit": 4097.0, "lot_size": 0.15, "status": "open", "scope": "breakout_m15", "mt5_ticket": 2}
    db.trades.rows.append(dict(tr))
    stamp = AsyncMock(return_value={"ok": True})
    cfg = {"exit_policy": "tp1_bank_runner", "tp1_close_fraction": 0.5, "be_plus_offset_price": 1.0, "tp2_multiple": 2.0}
    with patch("protection_guard.find_account", AsyncMock(return_value=({"_id": "a"}, None))), \
            patch("protection_guard.broker_stop_constraints", lambda acc, sym: {"min_stop_distance_px": 0.3}), \
            patch.object(TM, "stamp_pending_modification", stamp), patch.object(TM.ws_manager, "broadcast", AsyncMock()):
        assert _run(TM._tp1_bank_runner_exits(db, dict(tr), cfg, 4098.5)) is False      # TP1 not reached → engine rules continue
        row = db.trades.rows[0]
        assert row["tp_plan"]["tp1"] == 4097.0 and row["tp_plan"]["tp2"] == 4094.0
        assert _run(TM._tp1_bank_runner_exits(db, dict(row), cfg, 4097.0)) is True
    pend, extra = stamp.await_args.args[2], stamp.await_args.args[3]
    assert pend == {"type": "PARTIAL_CLOSE", "new_volume": 0.08, "new_sl": 4099.0, "new_tp": 4094.0, "reason": "tp1_bank"}
    assert extra["tp1_bank"]["close_volume"] == 0.07 and extra["tp1_closed"] is True and extra["scalp_exit_rule"] == "tp1_bank"
    # report split
    closed = {**row, "tp1_bank": {**extra["tp1_bank"], "pnl": 2.1}, "pnl": 5.0, "status": "closed"}
    assert X.split_pnl(closed) == {"banked_pnl": 2.1, "runner_pnl": 2.9, "policy": "tp1_bank_runner", "tp1_at": extra["tp1_bank"]["at"]}
    assert X.split_pnl({"pnl": -3.0}) == {"banked_pnl": 0.0, "runner_pnl": -3.0, "policy": "fixed_tp"}
    from scalp.reports import tp1_bank_split
    agg = tp1_bank_split([closed, {"pnl": -3.0}])
    assert agg == {"trades": 1, "banked_pnl": 2.1, "runner_pnl": 2.9, "total_pnl": 5.0, "sl_only": 0, "partial_failed_full_close": 0}


def test_72_fixed_tp_default_leaves_trade_manager_path_untouched():
    import trade_manager as TM
    src = open(TM.__file__).read()
    assert "if _tp1_applies(trade, cfg):" in src and 'applies as _tp1_applies' in src
    assert not X.applies({"scope": "hf_scalp", "status": "open"}, {"exit_policy": "fixed_tp"})
