"""iter-140 · Shadow model testing — pure-function tests."""
from datetime import datetime, timedelta, timezone

import pytest

from bayes_opt import new_replay_state
from model_shadow import (PROMOTE_MIN_DAYS, PROMOTE_MIN_PF,
                          PROMOTE_MIN_TRADES, _clamp_params, params_version,
                          promotion_status)
from strategy_engines import DEFAULT_PARAMS, PARAM_BOUNDS


def test_params_version_locked_and_distinct():
    v1 = params_version("hf_scalp", {"slope_min": 0.1, "mom_min": 0.12})
    v2 = params_version("hf_scalp", {"mom_min": 0.12, "slope_min": 0.1})
    assert v1 == v2                       # order-independent
    assert v1 != params_version("hf_scalp", {"slope_min": 0.11, "mom_min": 0.12})
    assert v1.startswith("hf_scalp~")


def test_clamp_params_enforces_bounds_and_fills_defaults():
    clean = _clamp_params("hf_scalp", {"slope_min": 99.0})
    lo, hi = PARAM_BOUNDS["hf_scalp"]["slope_min"]
    assert clean["slope_min"] == hi
    assert clean["mom_min"] == DEFAULT_PARAMS["hf_scalp"]["mom_min"]
    assert set(clean) == set(PARAM_BOUNDS["hf_scalp"])


def _model(days_ago: int, ch: dict, bl: dict) -> dict:
    return {
        "registered_at": (datetime.now(timezone.utc)
                          - timedelta(days=days_ago)).isoformat(),
        "challenger_state": {**new_replay_state(), **ch},
        "baseline_state": {**new_replay_state(), **bl},
    }


def test_promotion_blocked_on_young_model():
    m = _model(3, {"trades": 100, "total_r": 40.0, "gross_win_r": 60.0,
                   "gross_loss_r": 20.0}, {"total_r": 5.0})
    st = promotion_status(m)
    assert not st["ready"]
    days_check = next(c for c in st["checks"] if "days in shadow" in c["name"])
    assert not days_check["passed"]


def test_promotion_blocked_on_few_trades():
    m = _model(PROMOTE_MIN_DAYS + 1,
               {"trades": PROMOTE_MIN_TRADES - 1, "total_r": 20.0,
                "gross_win_r": 30.0, "gross_loss_r": 10.0}, {"total_r": 1.0})
    assert not promotion_status(m)["ready"]


def test_promotion_blocked_when_losing_to_baseline():
    m = _model(PROMOTE_MIN_DAYS + 1,
               {"trades": 50, "total_r": 10.0, "gross_win_r": 40.0,
                "gross_loss_r": 30.0},
               {"total_r": 15.0})
    st = promotion_status(m)
    assert not st["ready"]
    beat = next(c for c in st["checks"] if "beats production" in c["name"])
    assert not beat["passed"]


def test_promotion_blocked_on_low_profit_factor():
    m = _model(PROMOTE_MIN_DAYS + 1,
               {"trades": 50, "total_r": 2.0, "gross_win_r": 26.0,
                "gross_loss_r": 24.0},        # PF ≈ 1.08 < 1.25
               {"total_r": 0.0})
    st = promotion_status(m)
    assert st["profit_factor"] < PROMOTE_MIN_PF
    assert not st["ready"]


def test_promotion_ready_when_all_gates_pass():
    m = _model(PROMOTE_MIN_DAYS + 2,
               {"trades": 60, "total_r": 25.0, "gross_win_r": 50.0,
                "gross_loss_r": 25.0, "max_dd": 6.0},
               {"total_r": 5.0})
    st = promotion_status(m)
    assert st["ready"]
    assert all(c["passed"] for c in st["checks"])


def test_promotion_blocked_on_deep_drawdown():
    m = _model(PROMOTE_MIN_DAYS + 2,
               {"trades": 60, "total_r": 25.0, "gross_win_r": 50.0,
                "gross_loss_r": 25.0, "max_dd": 30.0},
               {"total_r": 5.0})
    assert not promotion_status(m)["ready"]
