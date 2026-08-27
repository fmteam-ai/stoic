"""iter-142 · Quant review round 3 — manual-route Kelly removal, setup
score, returns-based correlation, instrument-aware backtester costs."""
import math
import os
from datetime import datetime, timedelta, timezone

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 1) Manual trade route: no implicit Kelly ──────────────────────────────

def test_execute_route_has_no_kelly_scaling():
    src = open(os.path.join(BACKEND, "routes", "trade_routes.py")).read()
    ex = src.split("async def execute_signal")[1].split("async def ")[0]
    assert "kelly_f" not in ex
    assert "kelly_cap" not in ex
    assert "conf_scale" not in ex
    # plain hard ceiling remains
    assert "min(absolute_lot, max_lot_cap)" in ex


def test_execute_route_still_uses_fixed_fraction_sizing():
    src = open(os.path.join(BACKEND, "routes", "trade_routes.py")).read()
    ex = src.split("async def execute_signal")[1].split("async def ")[0]
    assert "compute_lot_for_account" in ex
    assert "kelly_enabled=True" not in ex   # default False = fixed fraction


# ── 2) Setup score ─────────────────────────────────────────────────────────

from setup_score import BASIS, SCORE_MAX, SCORE_MIN, compute_setup_score  # noqa: E402

STRONG_UP = {"momentum_3h_pct": 0.30, "ema20_slope_pct_2h": 0.20,
             "trend": "UP", "donchian20": "BREAK_UP", "range_pos_pct": 60,
             "day_range_pct": 1.0, "vwap_dist_pct": 0.1}


def test_score_bounds_and_basis():
    for feats in (STRONG_UP, {}, None):
        for action in ("BUY", "SELL"):
            s = compute_setup_score("hf_scalp", action, feats)
            assert SCORE_MIN <= s["score"] <= SCORE_MAX
            assert s["basis"] == BASIS


def test_aligned_setup_scores_higher_than_counter():
    with_ = compute_setup_score("hf_scalp", "BUY", STRONG_UP)
    against = compute_setup_score("hf_scalp", "SELL", STRONG_UP)
    assert with_["score"] > against["score"]
    assert with_["score"] >= 75          # strong confluence scores high
    assert against["score"] <= 45        # fighting everything scores low


def test_score_varies_not_constant():
    weak = compute_setup_score("hf_scalp", "BUY",
                               {"momentum_3h_pct": 0.02, "trend": "FLAT",
                                "day_range_pct": 0.1})
    assert weak["score"] != compute_setup_score("hf_scalp", "BUY", STRONG_UP)["score"]


def test_fade_entry_rewards_stretch_penalizes_trend():
    flat_stretch = compute_setup_score(
        "hf_scalp", "SELL",
        {"vwap_dist_pct": 0.5, "trend": "FLAT", "momentum_3h_pct": 0.0,
         "day_range_pct": 0.8, "range_pos_pct": 80},
        entry_style="fade")
    fading_trend = compute_setup_score(
        "hf_scalp", "SELL",
        {"vwap_dist_pct": 0.5, "trend": "UP", "momentum_3h_pct": 0.25,
         "day_range_pct": 0.8, "range_pos_pct": 80},
        entry_style="fade")
    assert flat_stretch["score"] > fading_trend["score"]


def test_mtf_alignment_bonus():
    feats = {"momentum_3h_pct": 0.10, "trend": "UP", "day_range_pct": 1.0}
    base = compute_setup_score("mtf_moderate", "BUY", feats)
    mtf = compute_setup_score("mtf_moderate", "BUY", feats,
                              mtf_conf={"aligned": True})
    assert mtf["score"] > base["score"]


def test_ai_signals_synthetic_confidence_removed():
    src = open(os.path.join(BACKEND, "ai_signals.py")).read()
    assert 'min_confidence"] + 5' not in src
    assert "compute_setup_score" in src


# ── 3) Returns-based correlation ───────────────────────────────────────────

from portfolio.var import MIN_OVERLAP, UNKNOWN_RHO, _pearson  # noqa: E402


def _aligned_corr(ra: dict, rb: dict):
    common = sorted(set(ra) & set(rb))
    if len(common) < MIN_OVERLAP:
        return None
    return _pearson([ra[d] for d in common], [rb[d] for d in common])


def test_aligned_returns_perfect_correlation():
    dates = [f"2026-06-{d:02d}" for d in range(1, 21)]
    ra = {d: math.sin(i) * 0.01 for i, d in enumerate(dates)}
    rb = {d: math.sin(i) * 0.02 for i, d in enumerate(dates)}   # 2× scaled
    assert _aligned_corr(ra, rb) == pytest.approx(1.0, abs=1e-6)


def test_misaligned_dates_only_overlap_counts():
    ra = {f"2026-06-{d:02d}": 0.01 * ((-1) ** d) for d in range(1, 25)}
    rb = {f"2026-07-{d:02d}": 0.01 * ((-1) ** d) for d in range(1, 25)}
    assert _aligned_corr(ra, rb) is None          # zero overlap → unknown


def test_insufficient_overlap_returns_none_and_prior_is_conservative():
    dates = [f"2026-06-{d:02d}" for d in range(1, MIN_OVERLAP)]  # one short
    ra = {d: 0.01 for d in dates}
    rb = {d: 0.01 for d in dates}
    assert _aligned_corr(ra, rb) is None
    assert 0 < UNKNOWN_RHO <= 1.0                 # unknown ≠ independence


def test_trending_levels_no_longer_spuriously_correlated():
    """Two independent random walks share a drift — LEVEL correlation reads
    ~1 (the old bug) while RETURN correlation stays near 0."""
    import random
    rng = random.Random(42)
    la, lb, ra, rb = [100.0], [100.0], {}, {}
    for i in range(60):
        xa, xb = rng.gauss(0.001, 0.01), rng.gauss(0.001, 0.01)
        la.append(la[-1] * (1 + xa))
        lb.append(lb[-1] * (1 + xb))
        d = f"d{i:03d}"
        ra[d], rb[d] = xa, xb
    level_corr = _pearson(la, lb)
    ret_corr = _aligned_corr(ra, rb)
    assert abs(ret_corr) < 0.4
    assert abs(ret_corr) < abs(level_corr) or abs(ret_corr) < 0.2


def test_consumers_use_returns_correlation():
    for rel in (os.path.join("portfolio", "correlation_kelly.py"),
                os.path.join("portfolio", "optimizer.py"),
                os.path.join("portfolio", "var.py")):
        src = open(os.path.join(BACKEND, rel)).read()
        assert "corr_returns" in src, rel
        assert "_close_series(s" not in src.split("def _close_series")[-1] \
            or rel.endswith("var.py")


# ── 4) Instrument-aware, cost-realistic backtester ────────────────────────

from backtester.engine import BarEvent, Engine, EngineConfig, OrderEvent  # noqa: E402

T0 = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _bar(i, o, h, l, c, sym="XAUUSD"):  # noqa: E741
    return BarEvent(ts=T0 + timedelta(minutes=15 * i), symbol=sym,
                    open=o, high=h, low=l, close=c, volume=0)


def _one_shot(sym, sl=None, tp=None):
    fired = {"n": 0}

    def strat(bar, macros, engine):
        if fired["n"] == 0:
            fired["n"] = 1
            return [OrderEvent(ts=bar.ts, symbol=sym, action="BUY",
                               lot_size=0.01, stop_loss=sl, take_profit=tp)]
        return []
    return strat


def test_entry_fill_uses_symbol_pip_not_fx_pip():
    eng = Engine(EngineConfig(slippage_pips=1.0, spread_map={"XAUUSD": 0.30}))
    eng.run([_bar(0, 4000, 4001, 3999, 4000),
             _bar(1, 4000, 4001, 3999, 4000)], _one_shot("XAUUSD"))
    fill = eng.result.fills[0]
    # BUY at mid 4000 + half-spread 0.15 + 1 pip × 0.10 = 4000.25
    assert fill.fill_price == pytest.approx(4000.25, abs=1e-6)
    # old bug: 4000 + 1 × 0.0001 = 4000.0001
    assert fill.fill_price != pytest.approx(4000.0001, abs=1e-6)


def test_btc_pip_is_one_dollar():
    eng = Engine(EngineConfig(slippage_pips=2.0, spread_map={"BTCUSD": 20.0}))
    eng.run([_bar(0, 60000, 60010, 59990, 60000, sym="BTCUSD"),
             _bar(1, 60000, 60010, 59990, 60000, sym="BTCUSD")],
            _one_shot("BTCUSD"))
    # 60000 + 10 (half-spread) + 2 × 1.0 (pips) = 60012
    assert eng.result.fills[0].fill_price == pytest.approx(60012.0)


def test_stop_exit_fills_through_the_level():
    eng = Engine(EngineConfig(slippage_pips=1.0, stop_slippage_mult=2.0,
                              spread_map={"XAUUSD": 0.30}))
    bars = [_bar(0, 4000, 4001, 3999, 4000),
            _bar(1, 4000, 4001, 3999, 4000),
            _bar(2, 3995, 3996, 3985, 3990)]   # SL 3990 touched
    eng.run(bars, _one_shot("XAUUSD", sl=3990.0, tp=4020.0))
    close = [f for f in eng.result.fills if f.action == "CLOSE"][0]
    # 3990 − stop slip (2×1 pip×0.10=0.20) − half-spread 0.15 = 3989.65
    assert close.fill_price == pytest.approx(3989.65, abs=1e-6)


def test_tp_limit_fill_pays_half_spread_only():
    eng = Engine(EngineConfig(slippage_pips=1.0, spread_map={"XAUUSD": 0.30}))
    bars = [_bar(0, 4000, 4001, 3999, 4000),
            _bar(1, 4000, 4001, 3999, 4000),
            _bar(2, 4005, 4025, 4004, 4020)]   # TP 4020 touched
    eng.run(bars, _one_shot("XAUUSD", sl=3980.0, tp=4020.0))
    close = [f for f in eng.result.fills if f.action == "CLOSE"][0]
    assert close.fill_price == pytest.approx(4020.0 - 0.15, abs=1e-6)


def test_flat_round_trip_is_net_negative():
    """Cost realism: entering and closing at an unchanged price must LOSE
    the spread+slippage — the old frictionless engine broke even."""
    eng = Engine(EngineConfig(slippage_pips=0.5, spread_map={"XAUUSD": 0.30}))
    state = {"step": 0}

    def strat(bar, macros, engine):
        state["step"] += 1
        if state["step"] == 1:
            return [OrderEvent(ts=bar.ts, symbol="XAUUSD", action="BUY",
                               lot_size=0.01)]
        if state["step"] == 3:
            return [OrderEvent(ts=bar.ts, symbol="XAUUSD", action="CLOSE")]
        return []
    bars = [_bar(i, 4000, 4000.5, 3999.5, 4000) for i in range(5)]
    res = eng.run(bars, strat)
    assert res.total_trades == 1
    assert res.total_pnl < 0


def test_unknown_symbol_defaults_conservative_fx_pip():
    eng = Engine(EngineConfig(slippage_pips=1.0, spread_map={}))
    eng.run([_bar(0, 1.10, 1.101, 1.099, 1.10, sym="EURNZD"),
             _bar(1, 1.10, 1.101, 1.099, 1.10, sym="EURNZD")],
            _one_shot("EURNZD"))
    assert eng.result.fills[0].fill_price == pytest.approx(1.10 + 0.0001, abs=1e-9)


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
