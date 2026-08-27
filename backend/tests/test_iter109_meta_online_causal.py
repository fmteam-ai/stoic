"""iter-109 · Meta-Learning + Online Learning + Causal AI tests."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import sys

sys.path.insert(0, _BACKEND_DIR)

from meta_strategy import (  # noqa: E402
    score_strategies, choose_strategy, STRATEGIES, SWITCH_MARGIN)
from online_learning import should_retrain  # noqa: E402
from causal_model import build_causal_view, PRESSURE_ACTIVE  # noqa: E402


# ---------- Meta-Learning ----------

def _t(strategy, pnl):
    return {"meta_strategy": strategy, "pnl": pnl}


def test_scores_reward_winning_strategy():
    trades = [_t("trend_rider", 50)] * 8 + [_t("scalper", -30)] * 8
    s = score_strategies(trades)
    assert s["trend_rider"]["mean"] > 0 > s["scalper"]["mean"]
    assert s["scalper"]["recent_losses"] == 5


def test_untried_strategies_keep_exploration_bonus():
    trades = [_t("trend_rider", 10)] * 20
    s = score_strategies(trades)
    assert s["breakout"]["n"] == 0
    assert s["breakout"]["ucb"] > 0  # pure exploration bonus


def test_switch_on_loss_streak():
    trades = ([_t("scalper", 40)] * 10          # scalper has history
              + [_t("trend_rider", 30)] * 5     # trend was fine…
              + [_t("trend_rider", -25)] * 4)   # …now bleeding
    s = score_strategies(trades)
    pick = choose_strategy(s, "trend_rider")
    assert pick["switched"] is True
    assert pick["strategy"] != "trend_rider"
    assert "lost" in pick["reason"]


def test_holds_when_current_performs():
    trades = [_t("trend_rider", 40)] * 12 + [_t("scalper", -20)] * 6
    s = score_strategies(trades)
    pick = choose_strategy(s, "trend_rider")
    assert pick["switched"] is False


def test_switch_on_sustained_outperformance():
    trades = [_t("trend_rider", -5)] * 10 + [_t("mean_reversion", 60)] * 10
    # interleave so trend's last-5 isn't all losses (mixed)
    trades = [x for pair in zip([_t("trend_rider", 5)] * 10,
                                [_t("mean_reversion", 60)] * 10) for x in pair]
    s = score_strategies(trades)
    if s["mean_reversion"]["ucb"] > s["trend_rider"]["ucb"] + SWITCH_MARGIN:
        pick = choose_strategy(s, "trend_rider")
        assert pick["switched"] is True and pick["strategy"] == "mean_reversion"


def test_initialises_without_history():
    pick = choose_strategy(score_strategies([]), None)
    assert pick["strategy"] in STRATEGIES


# ---------- Online Learning ----------

def test_retrain_on_new_trades():
    assert should_retrain(5, 60) is not None
    assert should_retrain(12, 60) is not None


def test_retrain_on_staleness_with_activity():
    assert should_retrain(1, 4000) is not None
    assert should_retrain(0, 999999) is None      # nothing new = no retrain
    assert should_retrain(2, 600) is None          # fresh + few = wait


# ---------- Causal AI ----------

def _macro(d_y10=0.0, d_infl=0.0, d_usd=0.0):
    return {"series": [
        {"series_id": "DGS10", "latest": 4.3, "wow_delta": d_y10},
        {"series_id": "T10YIE", "latest": 2.3, "wow_delta": d_infl},
        {"series_id": "DTWEXBGS", "latest": 121.0, "wow_delta": d_usd},
    ]}


def test_causal_bearish_gold_chain():
    # CPI hot: breakevens up, yields up MORE (real yields up), USD up
    v = build_causal_view(_macro(d_y10=0.20, d_infl=0.05, d_usd=0.8),
                          {"score": 0.6, "label": "hawkish"})
    assert v["pressure"] <= -PRESSURE_ACTIVE
    assert v["label"] == "BEARISH_GOLD"
    assert "PRESSURED" in v["narrative"]
    assert any(c["active"] for c in v["chain"])


def test_causal_bullish_gold_chain():
    # Yields collapsing faster than breakevens, USD sliding, dovish Fed
    v = build_causal_view(_macro(d_y10=-0.22, d_infl=-0.03, d_usd=-0.9),
                          {"score": -0.5, "label": "dovish"})
    assert v["pressure"] >= PRESSURE_ACTIVE
    assert v["label"] == "BULLISH_GOLD"


def test_causal_neutral_when_quiet():
    v = build_causal_view(_macro(0.01, 0.01, 0.05), {"score": 0.0})
    assert v["label"] == "NEUTRAL"
    assert abs(v["pressure"]) < PRESSURE_ACTIVE


def test_causal_real_yields_isolated():
    # Yields up purely from inflation (real yields flat) → little pressure
    v_infl = build_causal_view(_macro(d_y10=0.10, d_infl=0.10, d_usd=0.0), None)
    # Same nominal move but real (breakevens flat) → real pressure
    v_real = build_causal_view(_macro(d_y10=0.10, d_infl=0.0, d_usd=0.0), None)
    assert v_real["pressure"] < v_infl["pressure"]


def test_causal_none_without_series():
    assert build_causal_view(None) is None
    assert build_causal_view({"series": []}) is None


def test_consensus_macro_includes_causal():
    from consensus import compute_consensus
    base = {"action": "BUY", "confidence": 60}
    bull = compute_consensus({**base, "causal": {"pressure": 0.6}})
    bear = compute_consensus({**base, "causal": {"pressure": -0.6}})
    plain = compute_consensus(base)
    assert bull["votes"]["macro"] > plain["votes"]["macro"]
    assert bear["votes"]["macro"] < plain["votes"]["macro"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
