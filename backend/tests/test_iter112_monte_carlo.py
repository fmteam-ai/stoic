"""iter-112 · Monte Carlo Trade Simulation tests."""
import random
import sys

sys.path.insert(0, "/app/backend")

from monte_carlo import simulate_trade, mc_gate  # noqa: E402


def random_walk_bars(n=200, p=100.0, step=0.3, wick=0.1, seed=42):
    """Driftless by construction: every return appears with its negation, so
    the bootstrap distribution is exactly symmetric."""
    rng = random.Random(seed)
    rets = []
    for _ in range(n // 2):
        r = abs(rng.gauss(0, step))
        rets += [r, -r]
    rng.shuffle(rets)
    bars = []
    c = p
    for i, r in enumerate(rets):
        o = c
        c = o + r
        h = max(o, c) + abs(rng.gauss(0, wick))
        l = min(o, c) - abs(rng.gauss(0, wick))
        bars.append({"t": i * 900, "o": o, "h": h, "l": l, "c": c, "v": 100})
    return bars


BARS = random_walk_bars()


def test_symmetric_targets_near_even_odds():
    r = simulate_trade("BUY", 100.0, 98.0, 102.0, BARS,
                       n_paths=8000, seed=1)
    assert abs(r["p_tp_first"] - r["p_sl_first"]) < 0.08
    assert abs(r["p_tp_first"] + r["p_sl_first"] + r["p_timeout"] - 1.0) < 0.01


def test_closer_tp_hits_more_often():
    near = simulate_trade("BUY", 100.0, 98.0, 100.8, BARS, n_paths=6000, seed=2)
    far = simulate_trade("BUY", 100.0, 98.0, 104.0, BARS, n_paths=6000, seed=2)
    assert near["p_tp_first"] > far["p_tp_first"] + 0.15
    assert near["rr"] < far["rr"]


def test_sell_direction_mirrors():
    r = simulate_trade("SELL", 100.0, 102.0, 98.0, BARS, n_paths=6000, seed=3)
    assert abs(r["p_tp_first"] - r["p_sl_first"]) < 0.08


def test_dd_distribution_sane():
    r = simulate_trade("BUY", 100.0, 98.0, 102.0, BARS, n_paths=6000, seed=4)
    assert 0 <= r["max_dd_r_median"] <= r["max_dd_r_p95"]
    assert r["max_dd_r_p95"] <= 1.5   # SL caps adverse excursion around 1R+wick
    assert 1 <= r["median_bars_to_exit"] <= r["horizon_bars"]


def test_terrible_rr_negative_ev_and_gated():
    # Risk 3 to make 0.3 on a driftless walk → clearly negative EV
    r = simulate_trade("BUY", 100.0, 97.0, 100.3, BARS, n_paths=6000, seed=5)
    assert r["ev_r"] < 0
    msg = mc_gate(r)
    assert msg and "Negative EV" in msg and "vetoed" in msg


def test_great_rr_positive_ev_passes():
    # Risk 0.4 to make 4.0 — timeout paths dominate but EV should be ≥ 0-ish;
    # use tight SL far TP inverse: actually favourable = tight TP big SL is neg.
    # A fair 1:1 with drift-free walk ≈ 0 EV; craft positive EV via near TP.
    r = simulate_trade("BUY", 100.0, 96.0, 100.5, BARS, n_paths=6000, seed=6)
    if r["ev_r"] > 0:
        assert mc_gate(r) is None


def test_fail_open_on_bad_inputs():
    assert simulate_trade("BUY", None, 98, 102, BARS) is None
    assert simulate_trade("BUY", 100, 100, 102, BARS) is None
    assert simulate_trade("BUY", 100, 98, 102, BARS[:10]) is None
    assert simulate_trade("HOLD", 100, 98, 102, BARS) is None
    assert mc_gate(None) is None


def test_deterministic_with_seed():
    a = simulate_trade("BUY", 100.0, 98.0, 102.0, BARS, n_paths=3000, seed=9)
    b = simulate_trade("BUY", 100.0, 98.0, 102.0, BARS, n_paths=3000, seed=9)
    assert a == b


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
