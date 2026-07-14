"""iter-139 · RL capital allocator + portfolio optimizer — pure tests."""
import pytest

from rl_allocator import (MIN_TRADES, MIN_WEIGHT, build_allocations)
from portfolio.optimizer import target_weights


def test_insufficient_data_is_neutral():
    out = build_allocations({"hf_scalp": [-50.0] * (MIN_TRADES - 1)})
    assert out["hf_scalp"]["weight"] == 1.0
    assert "insufficient" in out["hf_scalp"]["reason"]


def test_positive_edge_keeps_full_budget():
    out = build_allocations({"hf_scalp": [10.0, 12.0, -3.0, 8.0, 15.0,
                                          -2.0, 9.0, 11.0, 7.0, 6.0, 14.0]})
    assert out["hf_scalp"]["weight"] == 1.0
    assert out["hf_scalp"]["p_positive"] > 0.5


def test_proven_loser_gets_floor_weight():
    pnls = [-20.0, -25.0, -18.0, -30.0, -22.0, -19.0,
            -28.0, -24.0, -21.0, -26.0, -23.0, -27.0]
    out = build_allocations({"range_fade": pnls})
    a = out["range_fade"]
    assert a["p_positive"] < 0.05
    assert a["weight"] == pytest.approx(MIN_WEIGHT, abs=0.05)


def test_ambiguous_scope_barely_shrunk():
    pnls = [10, -11, 9, -10, 12, -13, 8, -9, 11, -12, 10, -10]
    out = build_allocations({"hf_scalp_fast": [float(p) for p in pnls]})
    a = out["hf_scalp_fast"]
    assert 0.55 <= a["weight"] <= 1.0     # never punished hard on noise


def test_weights_never_inflate_above_one():
    out = build_allocations({
        "a": [100.0] * 50,
        "b": [-100.0] * 50,
        "c": [1.0, -1.0] * 25,
    })
    for a in out.values():
        assert MIN_WEIGHT <= a["weight"] <= 1.0


def test_weight_monotone_in_p_positive():
    strong_neg = build_allocations({"x": [-30.0] * 20})["x"]
    mild_neg = build_allocations({"x": [-5.0, 4.0] * 10})["x"]
    assert strong_neg["weight"] <= mild_neg["weight"]


# ── Portfolio optimizer target weights ────────────────────────────────────

def test_target_weights_inverse_vol():
    tw = target_weights({"XAUUSD": 1.0, "BTCUSD": 4.0}, {"XAUUSD": {}, "BTCUSD": {}})
    assert tw["XAUUSD"] > tw["BTCUSD"]
    assert sum(tw.values()) == pytest.approx(1.0, abs=0.001)


def test_target_weights_correlation_penalty():
    vols = {"A": 1.0, "B": 1.0, "C": 1.0}
    no_corr = target_weights(vols, {s: {} for s in vols})
    corr = {"A": {"B": 0.9, "C": 0.9}, "B": {"A": 0.9, "C": 0.0},
            "C": {"A": 0.9, "B": 0.0}}
    with_corr = target_weights(vols, corr)
    # A is correlated with both others → penalized below equal weight
    assert with_corr["A"] < no_corr["A"]


def test_target_weights_negative_corr_not_penalized():
    vols = {"A": 1.0, "B": 1.0}
    tw = target_weights(vols, {"A": {"B": -0.8}, "B": {"A": -0.8}})
    assert tw["A"] == pytest.approx(0.5, abs=0.01)
