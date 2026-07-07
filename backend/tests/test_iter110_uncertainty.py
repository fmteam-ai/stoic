"""iter-110 · Uncertainty Estimation tests."""
import sys

sys.path.insert(0, "/app/backend")

from uncertainty import estimate_uncertainty, uncertainty_gate  # noqa: E402


def _confident_signal():
    """Everything agrees, deep evidence, tight bands."""
    return {
        "action": "BUY", "confidence": 88,
        "consensus": {"score": 82, "votes": {"trend": 0.8, "quant": 0.7,
                      "structure": 0.9, "forecast": 0.6, "liquidity": 0.5,
                      "macro": 0.4}},
        "ml_ensemble": {"p_win": 0.86, "members": [
            {"name": "xgboost", "p": 0.88}, {"name": "lightgbm", "p": 0.85},
            {"name": "catboost", "p": 0.84}, {"name": "rl_agent", "p": 0.87}]},
        "bayes": {"p_success": 0.8, "ci90": [0.72, 0.87], "n": 45},
        "rl_policy": {"n": 30, "mean": 40},
        "forecast": {"median_change_pct": 0.8, "band_low_pct": 0.4,
                     "band_high_pct": 1.2},
    }


def _shaky_signal():
    """Models split, thin data, wide bands, agents conflicted."""
    return {
        "action": "BUY", "confidence": 54,
        "consensus": {"score": 51, "votes": {"trend": 0.3, "quant": -0.4,
                      "structure": -0.6, "forecast": 0.2, "liquidity": -0.3,
                      "macro": 0.1}},
        "ml_ensemble": {"p_win": 0.52, "members": [
            {"name": "xgboost", "p": 0.85}, {"name": "lightgbm", "p": 0.25},
            {"name": "catboost", "p": 0.6}, {"name": "rl_agent", "p": 0.35}]},
        "bayes": {"p_success": 0.5, "ci90": [0.2, 0.8], "n": 3},
        "rl_policy": {"n": 2, "mean": 5},
        "forecast": {"median_change_pct": 0.05, "band_low_pct": -1.0,
                     "band_high_pct": 1.1},
    }


def test_confident_signal_low_risk():
    est = estimate_uncertainty(_confident_signal())
    assert est["confidence_pct"] >= 75
    assert est["risk"] == "LOW"
    assert est["uncertainty"] <= 0.35


def test_shaky_signal_high_risk():
    est = estimate_uncertainty(_shaky_signal())
    assert est["risk"] == "HIGH"
    assert est["confidence_pct"] < 60
    assert len(est["drivers"]) >= 2


def test_calibration_shrinks_toward_50():
    est = estimate_uncertainty(_shaky_signal())
    assert abs(est["confidence_pct"] - 50) < abs(est["raw_direction_pct"] - 50) + 1


def test_disagreement_component():
    est = estimate_uncertainty(_shaky_signal())
    assert est["components"]["model_disagreement"] > \
        estimate_uncertainty(_confident_signal())["components"]["model_disagreement"]
    assert any("disagree" in d for d in est["drivers"])


def test_thin_evidence_flagged():
    est = estimate_uncertainty(_shaky_signal())
    assert any("past trades" in d or "thin evidence" in d for d in est["drivers"])


def test_gate_skips_high_risk():
    msg = uncertainty_gate(estimate_uncertainty(_shaky_signal()))
    assert msg and "skipped" in msg


def test_gate_passes_confident():
    assert uncertainty_gate(estimate_uncertainty(_confident_signal())) is None


def test_gate_respects_min_conf_floor():
    est = {"confidence_pct": 65, "risk": "MEDIUM", "uncertainty": 0.4,
           "drivers": []}
    assert uncertainty_gate(est, min_conf=60) is None
    assert uncertainty_gate(est, min_conf=70) is not None
    assert uncertainty_gate(None) is None


def test_empty_signal_defaults_sane():
    est = estimate_uncertainty({"action": "BUY"})
    assert 0 <= est["confidence_pct"] <= 100
    assert est["risk"] in ("LOW", "MEDIUM", "HIGH")


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
