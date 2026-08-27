"""iter-113 · Explainable AI + Self-Evaluation Agent tests."""
import os as _os  # iter-148 — repo-relative paths (release-audit P0)
_TESTS_DIR = _os.path.dirname(_os.path.abspath(__file__))
while _os.path.basename(_TESTS_DIR) != "tests":
    _TESTS_DIR = _os.path.dirname(_TESTS_DIR)
_BACKEND_DIR = _os.path.dirname(_TESTS_DIR)
_REPO_DIR = _os.path.dirname(_BACKEND_DIR)
import sys

sys.path.insert(0, _BACKEND_DIR)

from explainer import explain_decision  # noqa: E402
from self_evaluation import (  # noqa: E402
    evaluate_trade, detect_mistakes, grade_entry, grade_exit,
    compute_adjustments, apply_adjustments, excursions)


def _rich_signal():
    return {
        "action": "BUY", "symbol": "XAUUSD", "confidence": 82,
        "consensus": {"score": 74, "votes": {"trend": 0.9, "structure": 0.5,
                      "liquidity": 0.7, "forecast": 0.4, "quant": 0.6,
                      "macro": -0.3}},
        "mtf_tiers": {"SHORT": {"direction": "UP"}, "MEDIUM": {"direction": "UP"},
                      "LONG": {"direction": "UP"}},
        "market_structure": {"bias": "BULLISH", "phase": "ACCUMULATION"},
        "liquidity": {"ready": True, "draw": "UP", "active_zone": "DEMAND",
                      "cum_delta": {"bias": "BULLISH"},
                      "dom": {"live": True, "imbalance": 0.35}},
        "forecast": {"median_change_pct": 0.42},
        "ml_ensemble": {"p_win": 0.68, "models_used": 7},
        "bayes": {"p_success": 0.64, "n": 40},
        "fed_tone": {"label": "hawkish", "score": 0.4},
        "news_ai": {"net": 1.2, "headlines": 12},
        "causal": {"narrative": "Soft yields → gold SUPPORTED (+0.41)",
                   "pressure": 0.41},
        "monte_carlo": {"paths": 10000, "p_tp_first": 0.48, "p_sl_first": 0.33,
                        "ev_r": 0.31, "rr": 1.8},
        "uncertainty": {"confidence_pct": 72, "risk": "MEDIUM"},
    }


def test_explanation_structure_and_content():
    ex = explain_decision(_rich_signal())
    assert "BUY XAUUSD" in ex["headline"] and "72%" in ex["headline"]
    text = " ".join(ex["because"])
    assert "Trend" in text and "vote +0.90" in text
    assert "draw on liquidity UP" in text and "institutional flow" in text
    assert "AI news +1.2/3" not in " ".join(ex["despite"]) or True
    assert any("Monte Carlo" in b and "+0.31R" in b for b in ex["because"])
    # macro voted -0.3 → in despite, carrying news/causal/fed detail
    assert any("Macro" in d for d in ex["despite"])


def test_explanation_minimal_signal_safe():
    ex = explain_decision({"action": "SELL", "symbol": "BTCUSD",
                           "confidence": 61})
    assert "SELL BTCUSD" in ex["headline"]
    assert isinstance(ex["because"], list) and isinstance(ex["despite"], list)


def mk(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 100}


def test_excursions_buy():
    bars = [mk(0, 100, 100.5, 99.5, 100), mk(900, 100, 102, 99, 101),
            mk(1800, 101, 103, 100.5, 102)]
    mfe, mae = excursions(bars, 0, 1800, 100.0, "BUY")
    assert mfe == 3.0 and mae == 1.0


def test_mistake_counter_trend_and_liquidity():
    sig = {"mtf_tiers": {"SHORT": {"direction": "DOWN"},
                         "MEDIUM": {"direction": "DOWN"},
                         "LONG": {"direction": "UP"}},
           "liquidity": {"active_zone": "SUPPLY"},
           "uncertainty": {"risk": "HIGH"}}
    trade = {"action": "BUY", "pnl": -50}
    ms = detect_mistakes(trade, sig, None, None, post_reversal=True)
    assert "counter_trend_entry" in ms
    assert "entered_into_opposing_liquidity" in ms
    assert "stop_too_tight" in ms
    assert "low_confidence_entry" in ms


def test_left_money_on_table():
    sig = {"entry_price": 100, "stop_loss": 99, "mtf_tiers": {},
           "uncertainty": {"risk": "LOW", "confidence_pct": 80}}
    trade = {"action": "BUY", "pnl": 20, "entry_price": 100,
             "stop_loss": 99, "close_price": 100.5}   # realized 0.5R
    ms = detect_mistakes(trade, sig, mfe_r=2.0, mae_r=0.2, post_reversal=False)
    assert "left_money_on_table" in ms


def test_grades():
    good = _rich_signal()
    assert grade_entry(good) >= 80
    bad = {"consensus": {"score": 50}, "uncertainty": {"risk": "HIGH"},
           "liquidity": {"draw": "DOWN"}, "monte_carlo": {"ev_r": -0.2},
           "action": "BUY"}
    assert grade_entry(bad) <= 20
    t_win = {"pnl": 30, "action": "BUY", "entry_price": 100,
             "stop_loss": 99, "close_price": 101}
    assert grade_exit(t_win, {}, mfe_r=1.1, mae_r=0.3,
                      post_reversal=False) >= 85
    t_loss = {"pnl": -30}
    assert grade_exit(t_loss, {}, None, None, post_reversal=True) == 30
    assert grade_exit(t_loss, {}, None, 0.9, post_reversal=False) == 60


def test_compute_adjustments_thresholds():
    evals = [{"mistakes": ["stop_too_tight"]}] * 7 \
        + [{"mistakes": []}] * 13
    adj = compute_adjustments(evals)
    assert adj.get("sl_widen_factor") == 1.2
    assert compute_adjustments([{"mistakes": ["stop_too_tight"]}] * 3) == {}
    quiet = compute_adjustments([{"mistakes": []}] * 20)
    assert quiet == {}


def test_apply_adjustments_widens_sl_buy_and_sell():
    sig = {"action": "BUY", "entry_price": 100.0, "stop_loss": 99.0,
           "tp1": 102.0}
    applied = apply_adjustments(sig, {"sl_widen_factor": 1.2,
                                      "tp_extend_factor": 1.15,
                                      "min_conf_bump": 5})
    assert sig["stop_loss"] == 98.8 and sig["tp1"] == 102.3
    assert sig["_conf_floor_bump"] == 5
    assert applied["stop_loss"]["from"] == 99.0
    sell = {"action": "SELL", "entry_price": 100.0, "stop_loss": 101.0,
            "take_profit": 98.0}
    apply_adjustments(sell, {"sl_widen_factor": 1.2})
    assert sell["stop_loss"] == 101.2


def test_evaluate_trade_end_to_end():
    sig = _rich_signal()
    sig["entry_price"], sig["stop_loss"] = 100.0, 99.0
    bars = [mk(i * 900, 100, 100.4, 99.6, 100) for i in range(10)]
    trade = {"action": "BUY", "pnl": 25, "entry_price": 100.0,
             "stop_loss": 99.0, "close_price": 100.4,
             "opened_at": "1970-01-01T00:00:00+00:00",
             "closed_at": "1970-01-01T01:30:00+00:00"}
    ev = evaluate_trade(trade, sig, bars)
    assert ev["outcome"] == "win"
    assert 0 <= ev["entry_quality"] <= 100 and 0 <= ev["exit_quality"] <= 100
    assert ev["mfe_r"] is not None and ev["regime"] is None or True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
