"""iter-138 · Bayesian parameter optimization — pure-function tests."""
import math

import numpy as np
import pytest

from bayes_opt import (WARMUP, new_replay_state, replay, score_of,
                       suggest_next, _to_params, _from_params,
                       optimize_engine_params, proposal_id_of)
from strategy_engines import (DEFAULT_PARAMS, PARAM_BOUNDS, run_engine,
                              hf_scalp_signal, range_fade_signal,
                              breakout_signal)


# ── Parameterization must not change default behavior ─────────────────────

BURST_FEATS = {"atr15": 5.0, "trend": "UP", "last_price": 2000.0,
               "ema20": 1995.0, "ema20_slope_pct_2h": 0.12,
               "momentum_3h_pct": 0.15, "vwap_dist_pct": 0.5,
               "range_pos_pct": 60, "donchian20": "INSIDE",
               "recent_break": None, "day_range_pct": 0.5,
               "session_vwap": 1990.0}


def test_defaults_match_bounds_keys():
    for engine, bounds in PARAM_BOUNDS.items():
        assert set(bounds) == set(DEFAULT_PARAMS[engine])
        for k, (lo, hi) in bounds.items():
            assert lo <= DEFAULT_PARAMS[engine][k] <= hi


def test_params_none_equals_explicit_defaults():
    for feats in (BURST_FEATS,
                  {**BURST_FEATS, "trend": "FLAT", "vwap_dist_pct": 0.3}):
        for engine in PARAM_BOUNDS:
            a = run_engine(engine, feats, params=None)
            b = run_engine(engine, feats, params=dict(DEFAULT_PARAMS[engine]))
            assert a == b


def test_hf_scalp_momentum_burst_default():
    sig, note = hf_scalp_signal(BURST_FEATS)
    assert sig == "BUY" and "momentum burst" in note


def test_hf_scalp_param_override_blocks_burst():
    # raising slope_min above the feed's 0.12 kills the burst entry
    sig, _ = hf_scalp_signal(BURST_FEATS, params={"slope_min": 0.19,
                                                  "mom_min": 0.24})
    assert sig != "BUY" or "momentum burst" not in _


def test_breakout_min_day_rng_override():
    feats = {"atr15": 5.0, "donchian20": "BREAK_UP", "momentum_3h_pct": 0.2,
             "day_range_pct": 0.4}
    assert breakout_signal(feats)[0] == "BUY"            # default 0.3
    assert breakout_signal(feats, params={"min_day_rng": 0.6})[0] is None


def test_range_fade_edge_override():
    feats = {"trend": "FLAT", "donchian20": "INSIDE", "atr15": 2.0,
             "session_high": 2010.0, "session_low": 1990.0,
             "range_pos_pct": 25.0, "day_range_pct": 1.0,
             "recent_break": None, "session_vwap": 2000.0}
    assert range_fade_signal(feats)[0] is None           # 25% > default 20
    assert range_fade_signal(feats, params={"edge_pct": 28.0})[0] == "BUY"


# ── Replay bookkeeping ─────────────────────────────────────────────────────

def _bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1}


def test_replay_books_tp_and_sl_from_open_position():
    st = new_replay_state()
    st["open_pos"] = {"action": "BUY", "entry": 100.0, "sl": 99.0,
                      "tp": 102.0, "opened_t": 0}
    bars = [_bar(900 * i, 100, 100.5, 99.5, 100) for i in range(WARMUP + 1)]
    bars.append(_bar(900 * (WARMUP + 1), 100, 102.5, 99.5, 102))  # TP touch
    out = replay("breakout_m15", bars, [None] * len(bars), None,
                 start=len(bars) - 1, state=st)
    assert out["trades"] == 1 and out["wins"] == 1
    assert out["total_r"] == pytest.approx(2.0)
    assert out["open_pos"] is None


def test_replay_conservative_sl_first_when_both_touch():
    st = new_replay_state()
    st["open_pos"] = {"action": "BUY", "entry": 100.0, "sl": 99.0,
                      "tp": 102.0, "opened_t": 0}
    bars = [_bar(900 * i, 100, 100.5, 99.5, 100) for i in range(WARMUP + 1)]
    bars.append(_bar(900 * (WARMUP + 1), 100, 103.0, 98.0, 100))  # both touch
    out = replay("breakout_m15", bars, [None] * len(bars), None,
                 start=len(bars) - 1, state=st)
    assert out["losses"] == 1 and out["total_r"] == pytest.approx(-1.0)


def test_replay_incremental_equals_one_shot():
    """Splitting the replay into two continuations must equal one pass."""
    rng = np.random.default_rng(3)
    px = 2000.0
    bars = []
    for i in range(240):
        o = px
        px += float(rng.normal(0, 3))
        h, l = max(o, px) + 2, min(o, px) - 2
        bars.append(_bar(900 * i, o, h, l, px))
    from bayes_opt import precompute_features
    feats = precompute_features(bars)
    one = replay("breakout_m15", bars, feats, None)
    half = replay("breakout_m15", bars[:150], feats[:150], None)
    two = replay("breakout_m15", bars, feats, None, start=150, state=half)
    for k in ("trades", "wins", "losses"):
        assert one[k] == two[k]
    assert one["total_r"] == pytest.approx(two["total_r"])


def test_score_penalizes_small_samples_and_drawdown():
    st = new_replay_state()
    st["trades"], st["total_r"], st["max_dd"] = 2, 1.0, 0.0
    assert score_of(st, []) == pytest.approx(-1.0)       # 1.0 − 2.0 penalty
    st["trades"], st["max_dd"] = 10, 4.0
    assert score_of(st, []) == pytest.approx(1.0 - 2.0)  # DD penalty 0.5×4


# ── GP + EI ────────────────────────────────────────────────────────────────

def test_gp_ei_finds_1d_optimum():
    rng = np.random.default_rng(11)
    f = lambda x: -((x - 0.7) ** 2)          # max at 0.7
    X = [[float(rng.random())] for _ in range(4)]
    y = [f(x[0]) for x in X]
    for _ in range(14):
        nxt = suggest_next(X, y, 1, rng)
        X.append([float(nxt[0])])
        y.append(f(float(nxt[0])))
    best = X[int(np.argmax(y))][0]
    assert abs(best - 0.7) < 0.1


def test_param_encoding_roundtrip():
    bounds = PARAM_BOUNDS["hf_scalp"]
    names = sorted(bounds)
    p = dict(DEFAULT_PARAMS["hf_scalp"])
    x = _from_params(p, names, bounds)
    back = _to_params(x, names, bounds)
    for k in names:
        assert back[k] == pytest.approx(p[k], abs=1e-3)


def test_optimize_engine_params_end_to_end_synthetic():
    rng = np.random.default_rng(5)
    px = 2000.0
    bars = []
    for i in range(300):
        o = px
        px += float(rng.normal(0.5, 4))     # gentle uptrend with noise
        h, l = max(o, px) + 3, min(o, px) - 3
        bars.append(_bar(900 * i, o, round(h, 2), round(l, 2), round(px, 2)))
    res = optimize_engine_params("breakout_m15", bars, iters=4, init=3)
    assert res["evaluations"] == 8           # default + 3 init + 4 EI
    assert res["best"]["score"] >= res["default"]["score"]
    lo, hi = PARAM_BOUNDS["breakout_m15"]["min_day_rng"]
    assert lo <= res["best"]["params"]["min_day_rng"] <= hi


def test_proposal_id_stable_and_distinct():
    a = proposal_id_of("hf_scalp", {"slope_min": 0.1})
    assert a == proposal_id_of("hf_scalp", {"slope_min": 0.1})
    assert a != proposal_id_of("hf_scalp", {"slope_min": 0.11})
    assert a.startswith("hf_scalp~")
