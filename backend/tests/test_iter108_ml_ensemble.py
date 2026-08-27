"""iter-108 · Stacked ML Ensemble tests."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import math
import random
import shutil
import sys

sys.path.insert(0, _BACKEND_DIR)

from ml_ensemble import (  # noqa: E402
    featurize, train_sync, blend, ml_gate, _transformer_member,
    FEATURE_NAMES, MODEL_DIR, _load_models)

TEST_UID = "test-ensemble-uid"


def teardown_module():
    shutil.rmtree(MODEL_DIR / TEST_UID, ignore_errors=True)


def _sig(action="BUY"):
    return {"action": action, "confidence": 72,
            "mtf_tiers": {"SHORT": {"direction": "UP", "rsi": 61,
                                    "sma_fast_slope_pct": 0.4},
                          "MEDIUM": {"direction": "UP"},
                          "LONG": {"direction": "DOWN"}},
            "session": "london", "regime": "TRENDING",
            "entry_price": 2400.0, "stop_loss": 2395.0, "tp1": 2410.0}


def test_featurize_shape_and_values():
    x = featurize("BUY", "XAUUSD", _sig())
    assert len(x) == len(FEATURE_NAMES)
    d = dict(zip(FEATURE_NAMES, x))
    assert d["action_buy"] == 1.0 and d["confidence"] == 72
    assert d["tier_short"] == 1.0 and d["tier_long"] == -1.0
    assert d["align"] == 2 / 3
    assert d["sess_london"] == 1.0 and d["regime_trend"] == 1.0
    assert d["rr"] == 2.0  # 10 pips TP vs 5 pips SL
    assert featurize("SELL", "XAUUSD", {})[0] == 0.0  # empty sig safe


def _synthetic(n=400, seed=7):
    """Separable data: win iff confidence high AND aligned with trend."""
    rng = random.Random(seed)
    X, y = [], []
    for _ in range(n):
        sig = _sig()
        sig["confidence"] = rng.uniform(40, 95)
        up = rng.random() > 0.5
        sig["mtf_tiers"]["SHORT"]["direction"] = "UP" if up else "DOWN"
        action = rng.choice(["BUY", "SELL"])
        x = featurize(action, "XAUUSD", sig)
        aligned = (action == "BUY") == up
        p = 0.75 if (aligned and sig["confidence"] > 60) else 0.3
        X.append(x)
        y.append(1 if rng.random() < p else 0)
    return X, y


def test_train_sync_learns_and_weights_normalize():
    X, y = _synthetic()
    res = train_sync(X, y, TEST_UID)
    assert res["models_saved"] == 4
    assert set(res["aucs"]) == {"gradient_boosting", "xgboost",
                                "lightgbm", "catboost"}
    assert sum(1 for a in res["aucs"].values() if a > 0.55) >= 2
    total = sum(res["weights"].values())
    assert abs(total - 1.0) < 0.02 or total == 0.0
    models = _load_models(TEST_UID)
    assert len(models) == 4
    for mdl in models.values():
        p = float(mdl.predict_proba([featurize("BUY", "XAUUSD", _sig())])[0][1])
        assert 0.0 <= p <= 1.0


def test_blend_weighted_average():
    members = [{"name": "a", "p": 0.8, "w": 0.5}, {"name": "b", "p": 0.4, "w": 0.5}]
    assert abs(blend(members) - 0.6) < 1e-9
    assert blend([]) is None
    assert blend([{"name": "a", "p": 0.9, "w": 0.0}]) is None


def test_transformer_member_mapping():
    fc = {"last": 100.0, "q10": 100.5, "q50": 101.0, "q90": 102.0}  # band above
    assert _transformer_member(fc, "BUY") == 0.75
    assert _transformer_member(fc, "SELL") == 0.25
    fc2 = {"last": 100.0, "q10": 99.0, "q50": 100.4, "q90": 101.0}  # median up
    assert _transformer_member(fc2, "BUY") == 0.6
    assert _transformer_member(None, "BUY") is None


def test_ml_gate_thresholds():
    low = {"p_win": 0.32, "models_used": 5,
           "members": [{"name": "xgboost"}, {"name": "rl_agent"},
                       {"name": "bayesian"}, {"name": "transformer"},
                       {"name": "catboost"}]}
    msg = ml_gate(low)
    assert msg and "32%" in msg
    assert ml_gate({"p_win": 0.55, "models_used": 5, "members": []}) is None
    assert ml_gate({"p_win": 0.2, "models_used": 2, "members": []}) is None
    assert ml_gate(None) is None


def test_consensus_quant_includes_ensemble():
    from consensus import compute_consensus
    base = {"action": "BUY", "confidence": 60}
    up = compute_consensus({**base, "ml_ensemble": {"p_win": 0.8, "models_used": 5}})
    down = compute_consensus({**base, "ml_ensemble": {"p_win": 0.2, "models_used": 5}})
    plain = compute_consensus(base)
    assert up["votes"]["quant"] > plain["votes"]["quant"]
    assert down["votes"]["quant"] < plain["votes"]["quant"]
    ignored = compute_consensus({**base, "ml_ensemble": {"p_win": 0.9, "models_used": 1}})
    assert ignored["votes"]["quant"] == plain["votes"]["quant"]


def test_hour_features_cyclical():
    import datetime as dt
    x0 = featurize("BUY", "XAUUSD", _sig(),
                   dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.timezone.utc))
    x23 = featurize("BUY", "XAUUSD", _sig(),
                    dt.datetime(2026, 6, 15, 23, 0, tzinfo=dt.timezone.utc))
    i_sin = FEATURE_NAMES.index("hour_sin")
    dist = math.hypot(x0[i_sin] - x23[i_sin], x0[i_sin + 1] - x23[i_sin + 1])
    assert dist < 0.6  # 23:00 and 00:00 are neighbours on the circle


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
