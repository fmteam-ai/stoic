"""iter-139 · RL capital allocator + portfolio optimizer — pure tests.
Rewritten iter-103 for the evidence-floor policy (30/100 trades, Bayesian
shrinkage, gradual caps, tail-correlation control)."""
import pytest

from rl_allocator import (FULL_AUTHORITY_TRADES, LIMITED_FLOOR, MAX_STEP,
                          MIN_TRADES, MIN_WEIGHT, build_allocations)
from portfolio.optimizer import target_weights


def test_insufficient_data_is_neutral():
    out = build_allocations({"hf_scalp": [-50.0] * (MIN_TRADES - 1)})
    assert out["hf_scalp"]["weight"] == 1.0
    assert out["hf_scalp"]["authority"] == "none"
    assert "insufficient" in out["hf_scalp"]["reason"]


def test_positive_edge_keeps_full_budget():
    pnls = [10.0, 12.0, -3.0, 8.0, 15.0, -2.0, 9.0, 11.0] * 5   # n=40
    out = build_allocations({"hf_scalp": pnls})
    assert out["hf_scalp"]["weight"] == 1.0
    assert out["hf_scalp"]["p_shrunk"] > 0.5


def test_limited_authority_floor_is_half():
    pnls = [-20.0, -25.0, -18.0, -30.0] * 10                    # n=40 loser
    out = build_allocations({"range_fade": pnls})
    a = out["range_fade"]
    assert a["authority"] == "limited"
    assert LIMITED_FLOOR <= a["weight"] < 1.0


def test_full_authority_proven_loser_shrunk_harder():
    n = FULL_AUTHORITY_TRADES + 20
    out = build_allocations({"range_fade": [-25.0] * n})
    a = out["range_fade"]
    assert a["authority"] == "full"
    assert MIN_WEIGHT <= a["weight"] < LIMITED_FLOOR
    # shrinkage: even a certain loser never instantly hits the raw floor
    assert a["p_shrunk"] > 0.0


def test_bayesian_shrinkage_softens_small_samples():
    small = build_allocations({"x": [-25.0] * MIN_TRADES})["x"]
    large = build_allocations({"x": [-25.0] * 500})["x"]
    assert small["p_shrunk"] > large["p_shrunk"]
    assert small["weight"] >= large["weight"]


def test_weights_never_inflate_above_one():
    out = build_allocations({
        "a": [100.0] * 150,
        "b": [-100.0] * 150,
        "c": [1.0, -1.0] * 75,
    })
    for a in out.values():
        assert MIN_WEIGHT <= a["weight"] <= 1.0


def test_ci95_reported():
    out = build_allocations({"x": [10.0, -5.0] * 30})
    lo, hi = out["x"]["ci95"]
    assert lo < out["x"]["mean_pnl"] < hi


def test_gradual_cap_limits_step():
    n = FULL_AUTHORITY_TRADES + 20
    out = build_allocations({"x": [-25.0] * n}, prev_weights={"x": 1.0})
    a = out["x"]
    assert a["weight"] == pytest.approx(1.0 - MAX_STEP, abs=0.001)
    assert "gradual cap" in a["reason"]


def test_tail_correlation_trims_lower_evidence_scope():
    days = [f"2026-07-{d:02d}" for d in range(1, 16)]
    series = {d: 10.0 if i % 2 else -12.0 for i, d in enumerate(days)}
    pnls = [10.0, -5.0] * 25    # n=50, positive edge → weight 1.0
    out = build_allocations(
        {"a": pnls + pnls, "b": pnls},           # a has more evidence
        daily_by_scope={"a": dict(series), "b": dict(series)})
    assert out["a"]["weight"] == 1.0
    assert out["b"]["weight"] < 1.0
    assert "tail-correlation" in out["b"]["reason"]


def test_weight_monotone_in_evidence_strength():
    n = FULL_AUTHORITY_TRADES + 20
    strong_neg = build_allocations({"x": [-30.0] * n})["x"]
    mild_neg = build_allocations({"x": [-5.0, 4.0] * (n // 2)})["x"]
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


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
