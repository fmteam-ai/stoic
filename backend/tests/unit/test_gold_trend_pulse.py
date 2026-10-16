"""gold_trend_pulse (v1.60.57) — rules, modes, layering, range, sizing, live gate, pipeline hooks and guard coverage."""
import asyncio
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gold_trend_pulse import rules as R  # noqa: E402


def _run(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _bars(closes, t0=1_700_000_000, step=300, rng=0.4):
    out = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append({"t": t0 + i * step, "o": o, "h": max(o, c) + rng, "l": min(o, c) - rng, "c": c, "v": 1})
    return out


def _trend(n, start, slope):
    return [start + slope * i for i in range(n)]


# ------------------------------------------------------------------------------------------- 1. bias
def test_daily_bias_up_down_none():
    h4 = _bars(_trend(60, 3900, 2.0), step=14400); h1 = _bars(_trend(80, 3980, 0.5), step=3600)
    up = R.daily_bias(h4, h1, 4025.0, today_open=4010.0, prior_day_mid=4000.0)
    assert up["bias"] == "UP" and len(up["reasons"]) == 4 and all(r["ok"] for r in up["reasons"])
    h4d = _bars(_trend(60, 4100, -2.0), step=14400); h1d = _bars(_trend(80, 4020, -0.5), step=3600)
    dn = R.daily_bias(h4d, h1d, 3975.0, today_open=3990.0, prior_day_mid=4000.0)
    assert dn["bias"] == "DOWN"
    mixed = R.daily_bias(h4, h1, 4025.0, today_open=4030.0, prior_day_mid=4000.0)        # price below today's open
    assert mixed["bias"] == "NONE" and [r["ok"] for r in mixed["reasons"]] == [True, True, False, True]
    assert R.daily_bias(h4[:5], h1, 4025.0, 4010.0, 4000.0)["bias"] == "NONE"            # insufficient data


def test_bias_hysteresis_needs_two_consecutive_confirmations():
    tr = R.BiasTracker()
    now = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)
    assert tr.update({"bias": "UP", "reasons": []}, now) == "NONE"                        # 1st confirmation
    assert tr.update({"bias": "UP", "reasons": []}, now + timedelta(minutes=15)) == "UP"  # 2nd → flips
    assert tr.update({"bias": "DOWN", "reasons": []}, now + timedelta(minutes=30)) == "UP"
    assert tr.update({"bias": "NONE", "reasons": []}, now + timedelta(minutes=45)) == "UP"   # interrupted sequence resets
    assert tr.update({"bias": "NONE", "reasons": []}, now + timedelta(minutes=60)) == "NONE"
    assert not tr.due(now + timedelta(minutes=70)) and tr.due(now + timedelta(minutes=75))
    assert R.BiasTracker.from_dict(tr.to_dict()).bias == "NONE"


# ------------------------------------------------------------------------------------------- 2. entries
def _pullback_m5(up=True):
    """25 bars: trend, 3-bar pullback to the EMA20 with RSI dip, then a strong close with the trend."""
    s = 1 if up else -1
    closes = [4000 + s * 0.5 * i for i in range(21)]
    peak = closes[-1]
    closes += [peak - s * 3.5, peak - s * 5.5, peak - s * 5.0, peak - s * 4.2]   # deep pullback to the EMA20 (RSI7 < 40), close back with trend
    bars = _bars(closes, rng=0.3)
    return bars


def test_pullback_trigger_a_and_micro_breakout_trigger_b():
    bars = _pullback_m5(True)
    c = R.detect_entry(bars, "UP", vwap=None)
    assert c and c["direction"] == "BUY" and c["trigger"] in ("a", "b") and c["entry"] == bars[-1]["c"] and c["swing"] <= min(b["l"] for b in bars[-4:])
    # breakout: last close above the high of the 3 pullback bars → trigger b
    bars_b = _pullback_m5(True)
    for b in bars_b[-4:-1]:
        b["h"] = b["c"] + 0.05                                                             # tight pullback highs
    bars_b[-1]["c"] = max(b["h"] for b in bars_b[-4:-1]) + 0.1; bars_b[-1]["h"] = bars_b[-1]["c"] + 0.05
    assert R.detect_entry(bars_b, "UP", vwap=None)["trigger"] == "b"
    bars_a = _pullback_m5(True)
    bars_a[-1]["c"] = bars_a[-1]["o"] + 0.1; bars_a[-1]["h"] = bars_a[-1]["c"] + 0.1     # closes up, but below the pullback highs
    assert R.detect_entry(bars_a, "UP", vwap=None)["trigger"] == "a"
    dn = R.detect_entry(_pullback_m5(False), "DOWN", vwap=None)
    assert dn and dn["direction"] == "SELL"
    assert R.detect_entry(bars, "NONE", None) is None and R.detect_entry(bars, "DOWN", None) is None   # only in the bias direction


def test_spike_filter_blocks_chasing():
    bars = _pullback_m5(True)
    bars[-1]["h"] = bars[-1]["c"] + 8.0                                                   # range ≫ 2.5 × ATR14
    assert "spike" in R.detect_entry(bars, "UP", None)["blocked"]


# ------------------------------------------------------------------------------------------- 3. exits
def test_tp_sl_clamps_and_skip_when_sl_too_wide():
    assert R.plan_exit("BUY", 4000.0, 3998.5, atr14=1.0)["tp"] == 2.00                   # 0.7×1.0 clamped up to 2.00
    assert R.plan_exit("BUY", 4000.0, 3998.5, atr14=10.0)["tp"] == 4.50                  # clamped down
    p = R.plan_exit("BUY", 4000.0, 3998.5, atr14=4.0)                                     # tp 2.8, sl 1.5+0.3 = 1.8
    assert p["ok"] and p["sl"] == 1.8 and p["take_profit"] == 4002.8 and p["stop_loss"] == 3998.2 and p["be_trigger"] == 4001.68
    s = R.plan_exit("SELL", 4000.0, 4001.5, atr14=4.0)
    assert s["take_profit"] == 3997.2 and s["stop_loss"] == 4001.8
    wide = R.plan_exit("BUY", 4000.0, 3995.0, atr14=4.0)                                 # sl 5.3 > 1.3 × 2.8
    assert not wide["ok"] and "SL too wide for target" in wide["skip"]


def test_breakeven_sign_with_costs_and_time_stop():
    assert R.breakeven_level("BUY", 4000.0, spread=0.25, commission_price=0.07) == 4000.32
    assert R.breakeven_level("SELL", 4000.0, spread=0.25, commission_price=0.07) == 3999.68
    assert R.breakeven_due("BUY", 4000.0, 3.0, 4001.8) and not R.breakeven_due("BUY", 4000.0, 3.0, 4001.7)
    assert R.breakeven_due("SELL", 4000.0, 3.0, 3998.2) and not R.breakeven_due("SELL", 4000.0, 3.0, 3998.3)
    t0 = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
    assert not R.time_stop_due(t0, t0 + timedelta(minutes=44)) and R.time_stop_due(t0, t0 + timedelta(minutes=45))


def test_shadow_label_walks_m1_with_be_time_stop_and_costs():
    from gold_trend_pulse.engine import label_with_bars
    now = datetime(2026, 10, 16, 9, 0, tzinfo=timezone.utc)
    d = {"direction": "BUY", "entry": 4000.0, "take_profit": 4003.0, "stop_loss": 3998.0, "tp_dist": 3.0, "sl_dist": 2.0,
         "spread_at_entry": 0.25, "commission_price": 0.07, "created_at": now.isoformat(), "lots": 0.1}
    t0 = int(now.timestamp())
    tp_bars = [{"t": t0 + 60 * i, "o": 4000, "h": 4000.5 + 0.5 * i, "l": 4000.1 + 0.5 * i, "c": 4000.4 + 0.5 * i} for i in range(8)]
    lab = label_with_bars(d, tp_bars, now + timedelta(minutes=20))
    assert lab["exit_reason"] == "take_profit" and lab["net_price"] == 2.68 and lab["r"] == 1.34 and lab["pnl_usd"] == 26.8 and lab["result"] == "win"
    # 60 % of TP reached (4001.8) then back to entry → stop at BE (4000.32), small positive gross, net 0 after costs
    be_bars = [{"t": t0, "o": 4000, "h": 4001.9, "l": 3999.8, "c": 4001.5}, {"t": t0 + 60, "o": 4001.5, "h": 4001.6, "l": 4000.0, "c": 4000.1}]
    lab = label_with_bars(d, be_bars, now + timedelta(minutes=20))
    assert lab["exit_reason"] == "breakeven" and lab["exit_price"] == 4000.32 and lab["net_price"] == 0.0 and lab["be_armed"]
    flat = [{"t": t0 + 60 * i, "o": 4000, "h": 4000.3, "l": 3999.7, "c": 4000.1} for i in range(50)]
    lab = label_with_bars(d, flat, now + timedelta(minutes=60))
    assert lab["exit_reason"] == "time_stop" and lab["time_in_trade_min"] == 45.0 and lab["result"] == "loss"
    assert label_with_bars(d, flat[:5], now + timedelta(minutes=6)) is None                 # still pending


# ------------------------------------------------------------------------------------------- 4. filters
def test_filters_spread_session_cooldown_daily_stop_news():
    assert R.spread_ok(0.5, 2.0)[0] and not R.spread_ok(0.51, 2.0)[0]                   # ≤ 25 % of TP
    assert R.session_ok(datetime(2026, 10, 15, 7, 0, tzinfo=timezone.utc))[0]            # Thu 07:00
    assert not R.session_ok(datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc))[0]       # Thu 20:00
    assert R.session_ok(datetime(2026, 10, 16, 17, 59, tzinfo=timezone.utc))[0]          # Fri 17:59
    assert not R.session_ok(datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc))[0]       # Fri 18:00
    assert not R.session_ok(datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc))[0]       # Sat
    t = datetime(2026, 10, 16, 10, 0, tzinfo=timezone.utc)
    assert R.cooldown_until("win", t) == t + timedelta(minutes=3) and R.cooldown_until("loss", t) == t + timedelta(minutes=15)
    assert R.daily_stop(3, 0.0, 0) and R.daily_stop(0, -1.5, 0) and R.daily_stop(0, 0.0, 15) and R.daily_stop(2, -1.49, 14) is None
    ev = [{"country": "USD", "impact": "high", "when_ts": t.timestamp() + 14 * 60, "title": "CPI"}]
    assert R.news_blackout(ev, t) and R.news_blackout(ev, t + timedelta(minutes=29)) and not R.news_blackout(ev, t + timedelta(minutes=29, seconds=1))
    assert R.news_blackout([{"country": "EUR", "impact": "high", "when_ts": t.timestamp()}], t) is None
    assert R.news_blackout([{"country": "USD", "impact": "medium", "when_ts": t.timestamp()}], t) is None


# ------------------------------------------------------------------------------------------- 5. sizing
def test_sizing_fixed_quarter_percent_via_pip_value_guard():
    s = R.size_lots(10_000.0, 2.0, pip_usd_per_lot=10.0, pip_price=0.10)                 # $25 budget, $200/lot → 0.12
    assert s["ok"] and s["lots"] == 0.12 and s["planned_risk_pct"] == 0.25 and s["planned_risk_usd"] == 25.0 and s["risk_usd"] == 24.0
    assert R.size_lots(10_000.0, 2.0, 10.0, 0.10, max_lot=0.05)["lots"] == 0.05
    assert not R.size_lots(10_000.0, 2.0, 0.0, 0.10)["ok"]                                 # no authoritative pip value → blocked
    tiny = R.size_lots(300.0, 4.0, 10.0, 0.10)                                             # $0.75 budget vs $4 min-lot risk
    assert not tiny["ok"] and "min lot" in tiny["reason"]


# ------------------------------------------------------------------------------------------- 6. live gate
def test_live_gate_logic():
    good = {"n": 100, "expectancy_r": 0.1, "profit_factor": 1.3, "max_dd_r": 4.9}
    assert R.live_gate(good)["eligible"]
    for bad, needle in (({**good, "n": 99}, "99/100"), ({**good, "expectancy_r": 0.0}, "expectancy"),
                        ({**good, "profit_factor": 1.2}, "profit factor"), ({**good, "max_dd_r": 5.0}, "drawdown")):
        g = R.live_gate(bad)
        assert not g["eligible"] and any(needle in r for r in g["reasons"]), needle
    st = R.outcome_stats([{"r": 1.0, "pnl_usd": 10}, {"r": -1.0, "pnl_usd": -10}, {"r": 2.0, "pnl_usd": 20}, {"r": -0.5, "pnl_usd": -5}])
    assert st["n"] == 4 and st["win_rate"] == 0.5 and st["profit_factor"] == 2.0 and st["expectancy_r"] == 0.375 and st["max_dd_r"] == 1.0
    assert st["avg_win_usd"] == 15.0 and st["avg_loss_usd"] == -7.5


def test_mode_defaults_and_demo_live_falls_back_to_shadow_until_gate(monkeypatch):
    from gold_trend_pulse import engine as E
    from models import BotConfigUpdate
    cfg = BotConfigUpdate().model_dump()
    assert cfg["gold_trend_pulse_enabled"] is False and cfg["gold_trend_pulse_mode"] == "shadow" and cfg["gold_trend_pulse_layering_enabled"] is False
    assert not E.enabled_for(cfg) and E.requested_mode({"gold_trend_pulse_mode": "bogus"}) == "shadow"

    async def _gate_closed(db, account_id):
        return {"eligible": False, "reasons": ["0/100 labeled shadow trades"], "stats": {}}
    monkeypatch.setattr(E, "live_gate", _gate_closed)
    mode, gate = _run(E.effective_mode(None, {"gold_trend_pulse_mode": "demo_live"}, "a"))
    assert mode == "shadow" and "0/100" in gate["reasons"][0]


# ------------------------------------------------------------------------------------------- 11. layering
def test_layering_levels_shared_stop_and_basket_risk_cap():
    lv = R.layer_levels("BUY", ema20=4000.0, vwap=3999.0, atr14=2.0)
    assert lv == [("L1", 4000.0), ("L2", 3999.0), ("L3", 3998.0)]
    assert R.layer_levels("BUY", 4000.0, vwap=4001.0, atr14=2.0) == [("L1", 4000.0), ("L3", 3998.0)]   # VWAP above → not a deeper leg
    assert R.layer_levels("SELL", 4000.0, vwap=4001.0, atr14=2.0) == [("L1", 4000.0), ("L2", 4001.0), ("L3", 4002.0)]
    m15 = _bars([4000, 4001, 3998, 4002, 4003], step=900, rng=0.5)
    assert R.basket_shared_stop("BUY", m15) == 3997.2 and R.basket_shared_stop("SELL", m15) == 4003.8   # swing extreme ± 0.30
    assert R.leg_touched("BUY", {"l": 4000.25, "h": 4001}, 4000.0) and not R.leg_touched("BUY", {"l": 4000.31, "h": 4001}, 4000.0)
    s1 = R.size_basket_leg(10_000.0, 4000.0, 3998.0, 10.0, 0.10, risk_used_pct=0.0)
    s2 = R.size_basket_leg(10_000.0, 3999.0, 3998.0, 10.0, 0.10, risk_used_pct=s1["risk_pct"])
    s3 = R.size_basket_leg(10_000.0, 3998.5, 3998.0, 10.0, 0.10, risk_used_pct=s1["risk_pct"] + s2["risk_pct"])
    assert s1["ok"] and s2["ok"] and s3["ok"] and s1["risk_pct"] + s2["risk_pct"] + s3["risk_pct"] <= 0.75
    s4 = R.size_basket_leg(10_000.0, 3998.5, 3998.0, 10.0, 0.10, risk_used_pct=0.75)
    assert not s4["ok"] and "basket risk" in s4["reason"]
    assert R.basket_invalidated("BUY", "NONE", 4001.0, 3997.0) and R.basket_invalidated("BUY", "DOWN", 4001.0, 3997.0)
    assert R.basket_invalidated("BUY", "UP", 3996.9, 3997.0) and R.basket_invalidated("BUY", "UP", 3997.1, 3997.0) is None
    bs = R.basket_stats([{"legs": [{"pnl_usd": 10}, {"pnl_usd": -4}]}, {"legs": [{"pnl_usd": -30}, {"pnl_usd": -20}, {"pnl_usd": -10}]}], 10_000.0)
    assert bs["baskets"] == 2 and bs["legs_per_basket"] == 2.5 and bs["basket_win_rate"] == 0.5 and bs["worst_basket_loss_usd"] == -60.0 and bs["worst_basket_loss_pct"] == -0.6


def test_basket_counts_as_one_position_for_the_direction_cap():
    import user_direction_cap as U
    src = inspect.getsource(U.reserve_user_direction)
    assert '"gold_trend_pulse.basket_id": basket_id' in src and '"account_id": account_id' in src     # same account only
    assert "startswith(BASKET_SLOT_PREFIX)" in inspect.getsource(U.release_user_direction)
    br = open(os.path.join(BACKEND, "bot_runner.py")).read()
    assert 'basket_id=(signal.get("gold_trend_pulse") or {}).get("basket_id")' in br


# ------------------------------------------------------------------------------------------- 12. range mode
def _range_m5(n=30, lo=4000.0, hi=4006.0):
    import math
    mid, amp = (hi + lo) / 2, (hi - lo) / 2
    closes = [round(mid + amp * math.sin(2 * math.pi * i / 12), 2) for i in range(n)]   # smooth oscillation, peaks at i=3,15,27
    return _bars(closes, rng=0.0)


def test_range_detection_regime_and_breakout():
    m5 = _range_m5()
    now = m5[-1]["t"] + 300
    m15 = R.resample(m5, 900)
    rng = R.detect_range(m5, m15, now)
    assert rng and rng["hi"] == 4006.0 and rng["lo"] == 4000.0 and rng["mid"] == 4003.0 and rng["top"] == 4005.1 and rng["bottom"] == 4000.9
    assert R.regime("NONE", m5, m15, now)["regime"] == "RANGE" and R.regime("UP", m5, m15, now)["regime"] == "TREND"
    assert R.regime("NONE", _bars(_trend(30, 4000, 1.0)), [], now)["regime"] == "NO_TRADE"        # trending bars: no range
    assert R.detect_range(m5, [{"t": now - 600, "c": 4008.0}], now) is None                           # M15 close outside in last 30 min
    assert R.range_breakout({"c": 4006.5}, rng) == "UP" and R.range_breakout({"c": 3999.5}, rng) == "DOWN" and R.range_breakout({"c": 4003.0}, rng) is None
    assert R.wrong_side("SELL", "UP") and not R.wrong_side("BUY", "UP") and R.wrong_side("BUY", "DOWN")


def test_range_entries_and_exits():
    rng = {"hi": 4004.0, "lo": 4000.0, "mid": 4002.0, "atr": 1.0, "width": 4.0, "top": 4003.4, "bottom": 4000.6}
    sell = R.detect_range_entry({"o": 4003.3, "h": 4003.9, "l": 4003.1, "c": 4003.2}, rng)       # upper wick 0.6/0.8 ≥ 50 %
    assert sell and sell["direction"] == "SELL"
    assert R.detect_range_entry({"o": 4003.0, "h": 4003.9, "l": 4002.9, "c": 4003.8}, rng) is None  # closed in the top 15 % → no
    assert R.detect_range_entry({"o": 4003.5, "h": 4003.9, "l": 4002.0, "c": 4003.2}, rng) is None  # wick < 50 %
    buy = R.detect_range_entry({"o": 4000.7, "h": 4000.9, "l": 4000.1, "c": 4000.8}, rng)
    assert buy and buy["direction"] == "BUY"
    p = R.plan_range_exit("SELL", 4003.2, rng)                                                      # tp = min(1.2 to mid, clamp(0.5,1,3)=1.0) = 1.0
    assert p["ok"] and p["tp"] == 1.0 and p["sl"] == 1.1 and p["take_profit"] == 4002.2 and p["stop_loss"] == 4004.3 and p["time_stop_min"] == 20
    assert R.plan_range_exit("SELL", 4003.2, {**rng, "atr": 0.2})["tp"] == 1.0                   # clamp floor 1.00
    far = R.plan_range_exit("SELL", 4002.3, rng)                                                   # tp 0.3 (mid 4002.0), sl 2.0 → skip
    assert not far["ok"]


# ------------------------------------------------------------------------------------------- replay
def test_replay_runs_both_modes_and_reports_baskets():
    from gold_trend_pulse.replay import markdown, replay
    from gold_trend_pulse.synthetic import synthetic_m1
    bars = synthetic_m1(days=8, seed=3)
    rep = replay(bars, spread=0.25, commission_usd_per_lot_side=3.5)
    assert set(rep["by_mode"]) == {"TREND", "RANGE", "NEWS"} and rep["regime_minutes_per_day"] and rep["baskets"] is None
    for t in rep.get("overall", {}) and []:
        pass
    assert "| mode |" in markdown(rep)
    rep2 = replay(bars, spread=0.25, commission_usd_per_lot_side=3.5, layering=True)
    assert rep2["baskets"] is not None and (rep2["baskets"].get("baskets", 0) == 0 or rep2["baskets"]["worst_basket_loss_pct"] is not None)


# ------------------------------------------------------------------------------------------- pipeline hooks & guards (item 10)
def test_engine_registered_as_deterministic_scope_and_off_by_default():
    from strategy_engines import DETERMINISTIC_INTRADAY_SCOPES, ENGINE_LABELS, SCALP_ENGINES, DEFAULT_PARAMS
    assert "gold_trend_pulse" in DETERMINISTIC_INTRADAY_SCOPES and "gold_trend_pulse" in ENGINE_LABELS
    assert "gold_trend_pulse" not in SCALP_ENGINES and "gold_trend_pulse" not in DEFAULT_PARAMS   # existing engines untouched
    assert DEFAULT_PARAMS["hf_scalp_fast"] == {"slope_min": 0.05, "mom_min": 0.06, "exhaustion_vdist": 0.30, "flat_fade_min": 0.20}
    from risk_budget import SCOPE_TO_CLASS
    assert SCOPE_TO_CLASS["gold_trend_pulse"] == "scalp"
    ai = open(os.path.join(BACKEND, "ai_signals.py")).read()
    i = ai.index("if _gtp_on(freeze_cfg) and _base_sym(symbol) == _GTP_SYMBOL:")
    # the GTP branch sits AFTER market closure + macro freeze holds → both gates still apply to it
    assert ai.index('if macro.get("frozen"):') < i and ai.index("if market_closure:") < i


def test_pipeline_signal_shape_single_tp_fixed_risk():
    from gold_trend_pulse.engine import pipeline_signal
    sig = pipeline_signal({"action": "BUY", "entry_price": 4000.0, "stop_loss": 3998.2, "take_profit": 4002.8, "sl_pips": 18.0, "tp_pips": 28.0,
                           "trigger": "a", "decision_id": "d1", "planned_risk_usd": 25.0, "planned_risk_pct": 0.25, "spread_at_entry": 0.25,
                           "bias": "UP", "reason": "x", "regime": "TREND", "basket_id": None, "leg": None, "time_stop_min": 45}, {"symbol": "XAUUSD"})
    assert sig["scope"] == sig["engine"] == "gold_trend_pulse" and sig["tp_pips"] == [28.0] * 3 and sig["tp1"] == sig["tp3"] == 4002.8
    assert sig["risk_pct_cap"] == 0.25 and sig["tradeable"] and sig["gold_trend_pulse"]["regime"] == "TREND" and sig["stop_loss"] == 3998.2


def test_trade_manager_gtp_exits_be_60pct_time_stop_no_tiers(monkeypatch):
    import trade_manager as tm
    calls, closes = [], []

    async def _stamp(db, filt, mod, extra=None):
        calls.append((mod, extra)); return {"intent_id": "i", **mod}

    async def _noclamp(db, trade, new_sl, current, source):
        return None

    async def _rc(db, q, **kw):
        closes.append(kw); return {"trades_marked_for_close": 1}

    class _WS:
        async def broadcast(self, *a, **k):
            pass
    monkeypatch.setattr(tm, "stamp_pending_modification", _stamp); monkeypatch.setattr(tm, "be_clamped_skip", _noclamp)
    monkeypatch.setattr(tm, "request_close", _rc); monkeypatch.setattr(tm, "ws_manager", _WS())
    now = datetime.now(timezone.utc)
    buy = {"_id": "t", "user_id": "u", "symbol": "XAUUSD", "action": "BUY", "entry_price": 4000.0, "stop_loss": 3998.2, "lot_size": 0.12,
           "sl_pips": 18.0, "tp_pips": [28.0, 28.0, 28.0], "scope": "gold_trend_pulse", "spread_at_entry_pips": 2.5, "commission_pips": 0.7,
           "opened_at": (now - timedelta(minutes=10)).isoformat(), "gold_trend_pulse": {"regime": "TREND", "time_stop_min": 45}}
    assert _run(tm._gold_trend_pulse_exits(None, dict(buy), 4001.6, 16.0)) is False and not calls          # 57 % of TP
    assert _run(tm._gold_trend_pulse_exits(None, dict(buy), 4001.7, 17.0)) is True                        # 60.7 %
    assert calls[0][0]["type"] == "MODIFY_SL" and calls[0][0]["new_sl"] == 4000.32                        # entry + (2.5 + 0.7) pips
    sell = {**buy, "action": "SELL", "stop_loss": 4001.8}
    calls.clear()
    assert _run(tm._gold_trend_pulse_exits(None, dict(sell), 3998.3, 17.0)) is True and calls[0][0]["new_sl"] == 3999.68
    old = {**buy, "opened_at": (now - timedelta(minutes=45)).isoformat()}
    assert _run(tm._gold_trend_pulse_exits(None, dict(old), 4000.5, 5.0)) is True and closes[-1]["reason"] == "time_stop" \
        and closes[-1]["stamp"]["pending_modification"]["type"] == "FULL_CLOSE" and closes[-1]["stamp"]["time_stop_minutes"] == 45
    rng = {**buy, "gold_trend_pulse": {"regime": "RANGE", "time_stop_min": 20}, "opened_at": (now - timedelta(minutes=20)).isoformat()}
    assert _run(tm._gold_trend_pulse_exits(None, dict(rng), 4000.5, 5.0)) is True and closes[-1]["stamp"]["time_stop_minutes"] == 20
    src = inspect.getsource(tm.manage_trade) if hasattr(tm, "manage_trade") else open(os.path.join(BACKEND, "trade_manager.py")).read()
    i = src.index('== "gold_trend_pulse":')
    assert "return" in src[i:i + 200] and src.index("if is_scalp_engine_trade(trade):") > i          # exits-only, no tiers / BE-R / rescale


def test_every_existing_guard_still_applies_to_gold_trend_pulse():
    """item 10 — the engine produces a MAIN-PIPELINE signal; nothing in the guard chain exempts its scope."""
    br = open(os.path.join(BACKEND, "bot_runner.py")).read()
    for guard in ("in_friday_flat_window(cfg)", "anti_tilt_evaluate(db, user_id, cfg)", "eod_flatten_block(", "reserve_user_direction(",
                  "pip_value_anomaly", "asia_gold_skip_active()"):
        assert guard in br, guard
    assert "enforce_new_trade" in open(os.path.join(BACKEND, "execution.py")).read()      # authority / CLOSE_ONLY
    # only the generic signal cooldown and trade-of-day cap are replaced by the engine's own 3/15-min cooldown and 15/day cap
    assert br.count("_gtp_sym") == 3
    ex = open(os.path.join(BACKEND, "execution.py")).read()
    assert '"gold_trend_pulse": signal.get("gold_trend_pulse")' in ex
    # close paths: GTP live rows are ordinary trade rows → panic / friday_flat / eod_flatten / close_watchdog act on them
    import close_watchdog as cw
    assert not cw.is_mandatory_close("gtp_range_breakout") and not cw.is_mandatory_close("gtp_basket_invalidated")   # discretionary, expire-able
    ff = open(os.path.join(BACKEND, "friday_flat.py")).read()
    assert "gold_trend_pulse" not in ff and "gold_trend_pulse" not in open(os.path.join(BACKEND, "eod_flatten.py")).read()
    assert "gold_trend_pulse" not in open(os.path.join(BACKEND, "routes", "panic_routes.py")).read()
    # management on broker prices: GTP exits run after _broker_snapshot freshness check
    tm = open(os.path.join(BACKEND, "trade_manager.py")).read()
    assert tm.index("snap = await _broker_snapshot(trade)") < tm.index('== "gold_trend_pulse":')
    # a live decision is impossible while the gate is closed; shadow never writes a trade row
    eng = open(os.path.join(BACKEND, "gold_trend_pulse", "engine.py")).read()
    assert '"verdict": "live" if (mode == "demo_live" and sizing["ok"])' in eng and "db.trades.insert" not in eng
