"""iter-205 — STOIC v60 Execution Intelligence: Pre-Trade Digital Twin,
Execution Alpha, Broker Intelligence 2.0 matrix, Portfolio Risk Brain
2.0 (factor model + regime-sensitive correlations), Strategy Router 2.0
(recency + health), Uncertainty Engine 2.0 (conformal + components),
T0→T9 main-path telemetry completion."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


# ───────────────────── Execution Alpha (pure classifier) ─────────────────

def _classify(**over):
    from execution_alpha import classify
    base = {"spread_delay_ms": 0, "expected_slippage_pips": None,
            "typical_spread_pips": 3.0, "fill_grade": "A",
            "volatility": 0.4, "liquidity": 0.7, "lot": 0.1,
            "median_lot": 0.1, "is_scalp": False}
    return classify(**{**base, **over})


def test_alpha_normal_conditions_execute_now():
    p = _classify()
    assert p["mode"] == "EXECUTE_NOW" and p["risk_multiplier"] == 1.0


def test_alpha_scalp_slippage_kills_edge():
    p = _classify(expected_slippage_pips=8.0, is_scalp=True)
    assert p["mode"] == "SKIP" and p["risk_multiplier"] == 0.0


def test_alpha_swing_tolerates_same_slippage():
    p = _classify(expected_slippage_pips=8.0, is_scalp=False)
    assert p["mode"] != "SKIP"


def test_alpha_spread_spike_waits():
    p = _classify(spread_delay_ms=1200)
    assert p["mode"] == "WAIT"


def test_alpha_grade_d_reduces():
    p = _classify(fill_grade="D")
    assert p["mode"] == "REDUCE" and p["risk_multiplier"] == 0.7


def test_alpha_split_and_limit_advisory():
    p = _classify(lot=0.5, median_lot=0.1, volatility=0.9, liquidity=0.2)
    recs = {a["recommendation"] for a in p["advisory"]}
    assert {"SPLIT", "LIMIT"} <= recs
    assert p["mode"] == "EXECUTE_NOW"   # advisory never blocks alone


# ───────────────────── Pre-Trade Twin (fast + deep) ─────────────────

def test_twin_fast_edge_below_cost_hard_fails():
    from pretrade_twin import fast_checks
    f = fast_checks(risk_usd=50, equity=10000, ev_r=0.02,
                    required_edge_r=0.08, spread_stress=0.1)
    assert f["hard_fail"] is True


def test_twin_fast_gap_shock_reduces_not_blocks():
    from pretrade_twin import fast_checks
    f = fast_checks(risk_usd=150, equity=10000, ev_r=0.3,
                    required_edge_r=0.08, spread_stress=0.1)
    assert f["hard_fail"] is False
    assert f["fraction"] < 1.0   # 2x gap loss 300 > 2% (200) → scaled


def test_twin_fast_clean_trade_passes():
    from pretrade_twin import fast_checks
    f = fast_checks(risk_usd=50, equity=10000, ev_r=0.3,
                    required_edge_r=0.08, spread_stress=0.1)
    assert f["hard_fail"] is False and f["fraction"] == 1.0


def test_twin_deep_mc_deterministic_and_gates():
    from pretrade_twin import deep_mc
    good = deep_mc(0.6, 2.0)
    assert good["available"] and good["passed"] is True
    assert deep_mc(0.6, 2.0) == good
    bad = deep_mc(0.15, 1.0)
    assert bad["passed"] is False


# ───────────────── Broker Intelligence 2.0 matrix scoring ─────────────────

def test_matrix_cell_perfect_execution():
    from broker_intel import matrix_cell_score
    c = matrix_cell_score(slips=[0.1, 0.2], lats_ms=[200, 300],
                          rejects=0, total=10, typical_spread_pips=3.0)
    assert c["score"] >= 95


def test_matrix_cell_bad_execution_penalized():
    from broker_intel import matrix_cell_score
    c = matrix_cell_score(slips=[9.0, 12.0], lats_ms=[4000, 5000],
                          rejects=3, total=10, typical_spread_pips=3.0)
    assert c["score"] <= 40
    assert c["reject_rate"] == 0.3


# ───────────── Portfolio Brain 2.0 — factors + regime correlation ─────────

def test_regime_correlation_tightens_under_stress():
    from portfolio_risk import pair_correlation, regime_correlation
    base = pair_correlation("EURUSD", "GBPUSD")
    assert base == 0.65
    stressed = regime_correlation("EURUSD", "GBPUSD", vol_stress=1.0)
    assert stressed == 1.0
    neg = regime_correlation("EURUSD", "USDCHF", vol_stress=1.0)
    assert neg == -1.0
    assert regime_correlation("EURUSD", "GBPUSD", 0.0) == base


def test_factor_loadings_gold_and_fx():
    from portfolio_risk import factor_loadings
    gold = factor_loadings("XAUUSD")
    assert gold["GOLD"] == 1.0 and gold["USD"] < 0
    fx = factor_loadings("EURUSD")
    assert fx == {"EUR": 1.0, "USD": -1.0}


def test_marginal_factor_verdict_breach_reduces():
    from portfolio_risk import marginal_factor_verdict
    positions = [{"symbol": "XAUUSD", "action": "BUY", "lot": 1.0,
                  "entry_price": 2000.0, "stop_loss": 1998.0}]
    candidate = {"symbol": "XAUUSD", "action": "BUY", "lot": 2.0,
                 "entry_price": 2000.0, "stop_loss": 1998.0}
    v = marginal_factor_verdict(positions, candidate, equity=10000)
    assert v["breaches"]
    assert v["approved_fraction"] < 1.0
    assert "GOLD" in v["marginal_factors"]


def test_evaluate_carries_factor_verdict_and_vol_stress():
    from portfolio_risk import evaluate
    ev = evaluate([], {"symbol": "XAUUSD", "action": "BUY", "lot": 0.01,
                       "entry_price": 2000.0, "stop_loss": 1998.0},
                  equity=100000, vol_stress=0.8)
    assert "factor_verdict" in ev and ev["vol_stress"] == 0.8


# ───────────────────── Strategy Router 2.0 ─────────────────────

def test_router_recency_weight_half_life():
    from datetime import datetime, timedelta, timezone
    from strategy_router import recency_weight
    now = datetime.now(timezone.utc)
    fresh = recency_weight(now.isoformat())
    old = recency_weight((now - timedelta(days=21)).isoformat())
    assert fresh > 0.97
    assert abs(old - 0.5) < 0.02
    assert recency_weight("garbage") < 0.3


def test_router_health_multiplier_ordering():
    from strategy_router import HEALTH_ROUTER_MULT
    assert HEALTH_ROUTER_MULT["DISABLED"] < HEALTH_ROUTER_MULT["DEGRADED"] \
        < HEALTH_ROUTER_MULT["HEALTHY"]
    assert HEALTH_ROUTER_MULT["DISABLED"] >= 0.5   # router floors, never 0


# ───────────────────── Uncertainty 2.0 — conformal ─────────────────────

def test_conformal_interval_coverage_shape():
    from uncertainty_engine import conformal_interval
    rs = [0.5, -1.0, 1.5, 0.2, -0.4, 0.9, -1.0, 2.0, 0.1, -0.2] * 3
    ci = conformal_interval(rs)
    lo, hi = ci["interval"]
    assert lo <= ci["median_r"] <= hi
    inside = sum(1 for r in rs if lo <= r <= hi) / len(rs)
    assert inside >= 0.85   # ~90% coverage by construction
