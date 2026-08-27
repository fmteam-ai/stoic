"""iter-111 · Adaptive Position Sizing tests."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import sys

sys.path.insert(0, _BACKEND_DIR)

from adaptive_sizing import (  # noqa: E402
    confidence_mult, volatility_mult, accuracy_mult, liquidity_mult,
    drawdown_mult, combine)


def test_confidence_scaling_matches_examples():
    hi = confidence_mult({"confidence_pct": 98, "risk": "LOW"})
    lo = confidence_mult({"confidence_pct": 60, "risk": "HIGH"})
    assert hi >= 1.6           # 98% → presses toward 2×
    assert lo <= 0.3           # 60% + HIGH → quarter-size
    assert confidence_mult(None, None) == 1.0
    assert confidence_mult(None, 80) > confidence_mult(None, 60)


def mk(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c}


def test_volatility_inverse_targeting():
    calm = [mk(100, 101, 99, 100)] * 88 + [mk(100, 100.2, 99.8, 100)] * 8
    exploded = [mk(100, 100.5, 99.5, 100)] * 88 + [mk(100, 103, 97, 100)] * 8
    assert volatility_mult(calm) > 1.0       # compression → allowed larger
    assert volatility_mult(exploded) < 1.0   # expansion → cut size
    assert volatility_mult(None) == 1.0
    assert volatility_mult(calm[:10]) == 1.0


def test_accuracy_tiers():
    assert accuracy_mult(3, 12) == 0.5       # 20% WR → halve
    assert accuracy_mult(9, 11) == 0.75
    assert accuracy_mult(11, 9) == 1.0
    assert accuracy_mult(13, 7) == 1.15
    assert accuracy_mult(15, 5) == 1.3       # 75% WR → press
    assert accuracy_mult(2, 1) == 1.0        # <5 samples neutral


def test_liquidity_alignment():
    aligned = {"ready": True, "dom": {"live": True, "imbalance": 0.4},
               "draw": "UP"}
    against = {"ready": True, "dom": {"live": True, "imbalance": -0.4},
               "draw": "DOWN"}
    assert liquidity_mult(aligned, "BUY") > 1.0
    assert liquidity_mult(against, "BUY") < 1.0
    assert liquidity_mult(None, "BUY") == 1.0


def test_drawdown_throttle():
    m, dd = drawdown_mult([500, -300, -900], equity=10000)  # peak 500 → -700
    assert dd == 0.12 and m == 0.4
    m2, dd2 = drawdown_mult([100, 200, 50], equity=10000)   # near peak
    assert m2 == 1.0 and dd2 < 0.02
    assert drawdown_mult([], 10000) == (1.0, 0.0)


def test_combine_examples_from_spec():
    # Confidence 98 · everything favourable → capped at the Phase-1 1.3% cap
    strong = combine({"confidence": 1.8, "volatility": 1.1, "accuracy": 1.15,
                      "liquidity": 1.05, "drawdown": 1.0}, base_risk_pct=1.0)
    assert strong["risk_pct"] == 1.3          # hits the 1.3% cap (user bound)
    # Confidence 60 · HIGH risk · cold streak → floored at 0.25%
    weak = combine({"confidence": 0.29, "volatility": 1.0, "accuracy": 0.75,
                    "liquidity": 0.9, "drawdown": 1.0}, base_risk_pct=1.3)
    assert 0.25 <= weak["risk_pct"] <= 0.3
    # Floor holds
    tiny = combine({"confidence": 0.1, "drawdown": 0.4}, base_risk_pct=1.0)
    assert tiny["risk_pct"] == 0.25


def test_combine_clamps_multiplier():
    out = combine({"a": 5.0, "b": 5.0}, base_risk_pct=1.0)
    assert out["multiplier"] == 2.0
    out2 = combine({"a": 0.01}, base_risk_pct=1.0)
    assert out2["multiplier"] == 0.1


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
