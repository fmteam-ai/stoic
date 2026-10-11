"""Strategy DSL engine — candle features, rule evaluation, simulator mechanics and plain-English read-back."""
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit

import dsl_engine as de  # noqa: E402

H = 3600


def _bar(i, o, h, lo, c, step=900, t0=8 * H):
    return {"ts": t0 + i * step, "o": o, "h": h, "l": lo, "c": c}


def _dsl(entry, exits=None, params=None):
    return {"version": "1.0", "name": "t", "symbols": ["XAUUSD"], "params": params or {"rsi_oversold": 30},
            "entry_rules": entry,
            "exit_rules": exits or [{"kind": "take_profit_pct", "value": 1.0}, {"kind": "stop_loss_pct", "value": 0.5}],
            "pseudocode": ""}


ALWAYS_BUY = [{"field": "atr_pct", "op": ">", "value": 0, "side": "BUY"}]


def _flat(n, px=100.0):
    return [_bar(i, px, px + 0.05, px - 0.05, px) for i in range(n)]


def test_normalize_accepts_both_shapes_and_drops_junk():
    rows = [{"t": 900, "o": 1, "h": 2, "l": 0.5, "c": 1.5},
            {"date": "2026-01-02", "open": 1, "high": 2, "low": 0.5, "close": 1.5},
            {"t": 1, "o": 0, "h": 1, "l": 0, "c": 1}, {"foo": 1}]
    bars = de.normalize_bars(rows)
    assert len(bars) == 2 and bars[0]["ts"] < bars[1]["ts"]


def test_rsi_extremes_and_warmup():
    up = [_bar(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)]
    f = de.feature_series(up, intraday=True)
    assert f[13]["rsi_14"] is None and f[14]["rsi_14"] == pytest.approx(100.0)
    assert f[12]["atr_pct"] is None and f[14]["atr_pct"] > 0
    assert f[198]["ma_20_vs_200"] is None if len(f) > 198 else f[-1]["ma_20_vs_200"] is None


def test_ma_gap_sign():
    bars = [_bar(i, 100 + i * 0.1, 100.2 + i * 0.1, 99.8 + i * 0.1, 100 + i * 0.1) for i in range(220)]
    f = de.feature_series(bars, intraday=True)
    assert f[198]["ma_20_vs_200"] is None and f[199]["ma_20_vs_200"] > 0


def test_sessions_by_utc_hour():
    assert de._sessions_at(9 * H) == {"london"}
    assert de._sessions_at(13 * H) == {"london", "ny"}
    assert de._sessions_at(2 * H) == {"tokyo"}


def test_rule_true_variants():
    feat = {"rsi_14": 25.0, "atr_pct": 0.8, "ma_20_vs_200": 1.2, "session": {"london"}}
    p = {"rsi_oversold": 30}
    assert de.rule_true({"field": "rsi_14", "op": "<", "value_ref": "params.rsi_oversold"}, feat, p) is True
    assert de.rule_true({"field": "atr_pct", "op": "between", "value": [1, 0.5]}, feat, p) is True
    assert de.rule_true({"field": "ma_20_vs_200", "op": "=", "value": "above"}, feat, p) is True
    assert de.rule_true({"field": "ma_20_vs_200", "op": "=", "value": "bearish"}, feat, p) is False
    assert de.rule_true({"field": "session", "op": "in", "value": ["ny", "london"]}, feat, p) is True
    assert de.rule_true({"field": "session", "op": "=", "value": "tokyo"}, feat, p) is False
    assert de.rule_true({"field": "rsi_14", "op": "<", "value": "abc"}, feat, p) is False
    assert de.rule_true({"field": "rsi_14", "op": "<", "value": 30}, {"rsi_14": None}, p) is None


def test_fill_is_next_bar_open_and_take_profit_hits():
    # ATR(14) is first known at bar 14's close → the order fills at bar 15's open
    bars = _flat(15) + [_bar(15, 100.2, 100.5, 100.1, 100.4), _bar(16, 100.4, 101.5, 100.3, 101.4)]
    res = de.simulate(_dsl(ALWAYS_BUY), bars)
    t = res["trades"][0]
    assert t["entry"] == 100.2 and t["entry_ts"] == bars[15]["ts"]
    assert t["reason"] == "take_profit" and t["exit"] == pytest.approx(100.2 * 1.01)
    assert t["return_pct"] == pytest.approx(1.0 - 0.04)                   # 2 bps × 2 sides


def test_same_bar_stop_and_target_counts_as_stop():
    bars = _flat(16) + [_bar(16, 100, 101.5, 99.0, 100)]
    t = de.simulate(_dsl(ALWAYS_BUY), bars, cost_bps=0)["trades"][0]
    assert t["reason"] == "stop_loss" and t["return_pct"] == pytest.approx(-0.5)


def test_gap_through_stop_fills_at_open():
    bars = _flat(16) + [_bar(16, 100, 100.1, 99.9, 100), _bar(17, 98, 98.5, 97.5, 98)]
    t = de.simulate(_dsl(ALWAYS_BUY), bars, cost_bps=0)["trades"][0]
    assert t["exit"] == 98 and t["return_pct"] == pytest.approx(-2.0)


def test_breakeven_and_trailing_stop():
    exits = [{"kind": "stop_loss_pct", "value": 1.0}, {"kind": "breakeven_after_pct", "value": 0.5}]
    bars = _flat(16) + [_bar(16, 100, 100.7, 99.95, 100.6), _bar(17, 100.6, 100.6, 99.8, 99.9)]
    t = de.simulate(_dsl(ALWAYS_BUY, exits), bars, cost_bps=0)["trades"][0]
    assert t["reason"] == "breakeven" and t["return_pct"] == pytest.approx(0.0)

    exits = [{"kind": "stop_loss_pct", "value": 1.0}, {"kind": "trail_pct", "value": 0.5}]
    bars = _flat(16) + [_bar(16, 100, 102.1, 99.95, 102), _bar(17, 102, 102.05, 101.2, 101.3)]
    t = de.simulate(_dsl(ALWAYS_BUY, exits), bars, cost_bps=0)["trades"][0]
    assert t["reason"] == "trailing_stop" and t["exit"] == pytest.approx(102 * 0.995)


def test_short_side_mechanics():
    sell = [{"field": "atr_pct", "op": ">", "value": 0, "side": "SELL"}]
    bars = _flat(16) + [_bar(16, 100, 100.1, 98.8, 99)]
    t = de.simulate(_dsl(sell), bars, cost_bps=0)["trades"][0]
    assert t["side"] == "SELL" and t["reason"] == "take_profit" and t["return_pct"] == pytest.approx(1.0)


def test_any_rules_alone_never_trade_and_untestable_rules_are_reported():
    dsl = _dsl([{"field": "atr_pct", "op": ">", "value": 0, "side": "ANY"},
                {"field": "confidence", "op": ">", "value": 70, "side": "BUY"}])
    res = de.simulate(dsl, _flat(60))
    assert res["trades_n"] == 0
    ex = de.explain(dsl)
    assert "Buy: never" in ex["text"] and "Sell: never" in ex["text"]
    assert any("Not backtested" in w and "confidence" in w.lower() for w in ex["warnings"])
    assert "Live-only conditions" in ex["text"] and "AI signal confidence is above 70" in ex["text"]


def test_session_rule_untestable_on_daily_only():
    dsl = _dsl([{"field": "session", "op": "=", "value": "london", "side": "BUY"}])
    assert not de.explain(dsl, intraday=True)["untestable"]
    assert de.explain(dsl, intraday=False)["untestable"]


def test_explain_reads_back_exact_rules_and_flags_problems():
    dsl = _dsl([{"field": "rsi_14", "op": "<", "value_ref": "params.rsi_oversold", "side": "BUY"},
                {"field": "rsi_14", "op": ">", "value": 70, "side": "BUY"},
                {"field": "ma_20_vs_200", "op": "=", "value": "above", "side": "ANY"}],
               exits=[{"kind": "take_profit_pct", "value": 0.4}, {"kind": "stop_loss_pct", "value": 0.8}])
    ex = de.explain(dsl)
    assert "RSI(14) is below 30 (rsi_oversold)" in ex["text"]
    assert "MA(20) is above MA(200)" in ex["text"]
    assert "take profit at +0.4%" in ex["text"] and "stop loss at −0.8%" in ex["text"]
    assert any("can never enter" in w for w in ex["warnings"])
    assert any("win rate must exceed 67%" in w for w in ex["warnings"])


def test_missing_stop_loss_warned():
    ex = de.explain(_dsl(ALWAYS_BUY, exits=[{"kind": "take_profit_pct", "value": 1}]))
    assert "No stop loss — losses are unbounded." in ex["warnings"]


def test_one_position_at_a_time_and_metrics():
    rnd = random.Random(1)
    px, bars = 100.0, []
    for i in range(600):
        o = px
        px *= 1 + rnd.gauss(0, 0.003)
        bars.append(_bar(i, o, max(o, px) * 1.001, min(o, px) * 0.999, px))
    res = de.simulate(_dsl(ALWAYS_BUY), bars)
    ts = [(t["entry_ts"], t["exit_ts"]) for t in res["trades"]]
    assert all(a[1] <= b[0] for a, b in zip(ts, ts[1:]))                 # never overlapping
    assert res["trades_n"] == res["wins"] + res["losses"] > 30
    assert 0 <= res["max_drawdown_pct"] < 100 and res["exit_reasons"]


def test_backtest_payload_and_short_history():
    out = de.backtest(_dsl(ALWAYS_BUY), [{"t": i * 900, "o": 1, "h": 1.1, "l": 0.9, "c": 1} for i in range(10)], "M15")
    assert out["ready"] is False and out["explanation"]["text"]
    rows = [{"t": 8 * H + i * 900, "o": 100, "h": 100.05, "l": 99.95, "c": 100} for i in range(100)]
    out = de.backtest(_dsl([{"field": "ma_20_vs_200", "op": ">", "value": 0, "side": "BUY"}]), rows, "M15")
    assert out["ready"] and any("MA(200)" in n for n in out["notes"])
    assert any("too few" in n for n in out["notes"])
