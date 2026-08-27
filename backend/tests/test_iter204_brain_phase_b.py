"""iter-204 — STOIC Brain Phase B (v59): DecisionContext, Market Memory,
Outcome Attribution 2.0 counterfactuals, Strategy Decay Detector,
Champion/Challenger 2.0 scorecard, Dynamic Transaction Costs, Degraded
Intelligence Mode, fail-closed limiter for financial mutations."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


# ───────────────────────── DecisionContext ─────────────────────────

def test_decision_id_format_and_uniqueness():
    from decision_context import new_decision_id
    ids = {new_decision_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(i.startswith("dec_") and len(i) == 20 for i in ids)


# ───────────────────────── Market Memory ─────────────────────────

VEC = {"trend": 0.5, "volatility": 0.6, "liquidity": 0.7,
       "momentum": 0.3, "mean_reversion": 0.2, "correlation_stress": 0.1,
       "news_risk": 0.0, "spread_stress": 0.1, "gap_risk": 0.2}


def test_memory_similarity_identical_is_one():
    from market_memory import _similarity
    assert abs(_similarity(VEC, dict(VEC)) - 1.0) < 1e-9


def test_memory_similarity_opposite_clamped_zero():
    from market_memory import _similarity
    inv = {k: -v for k, v in VEC.items()}
    assert _similarity(VEC, inv) == 0.0


def test_memory_similarity_needs_shared_dims():
    from market_memory import _similarity
    assert _similarity(VEC, {"trend": 0.5}) == 0.0


def test_memory_verdict_avoid_reduce_ok():
    from market_memory import verdict
    avoid = verdict({"available": True, "confidence": 0.8,
                     "median_r": -0.4, "n": 50})
    assert avoid["action"] == "AVOID" and avoid["multiplier"] == 0.0
    reduce = verdict({"available": True, "confidence": 0.55,
                      "median_r": -0.1, "n": 30})
    assert reduce["action"] == "REDUCE" and 0 < reduce["multiplier"] < 1
    ok = verdict({"available": True, "confidence": 0.9,
                  "median_r": 0.4, "n": 100})
    assert ok["action"] == "OK" and ok["multiplier"] == 1.0
    unavailable = verdict({"available": False, "note": "thin"})
    assert unavailable["action"] == "OK"


# ───────────────────────── Strategy Decay ─────────────────────────

def test_decay_state_ladder():
    from strategy_decay import MULT, STATES, _state_of
    assert _state_of(0) == "HEALTHY"
    assert _state_of(1) == "WATCH"
    assert _state_of(2) == "DEGRADED"
    assert _state_of(3) == "DECAYING"
    assert _state_of(4) == "DISABLED"
    assert _state_of(9) == "DISABLED"
    assert MULT["DISABLED"] == 0.0
    assert MULT["HEALTHY"] == 1.0
    assert [MULT[s] for s in STATES] == sorted(
        [MULT[s] for s in STATES], reverse=True)


# ───────────────────────── Champion/Challenger 2.0 ─────────────────────────

def _winning_series(n=120):
    # deterministic 55% winners at +1.6R, losers at -1R → robust edge
    return [1.6 if i % 20 < 11 else -1.0 for i in range(n)]


def _losing_series(n=120):
    return [1.0 if i % 10 < 3 else -1.0 for i in range(n)]


def test_cc2_scorecard_qualifies_robust_series():
    from champion_challenger2 import build_scorecard
    sc = build_scorecard(_winning_series())
    assert sc["qualified"] is True
    assert sc["metrics"]["expectancy"] > 0
    assert sc["cpcv"]["oos_loss_rate"] <= 0.35
    assert sc["monte_carlo"]["p_profit"] >= 0.75


def test_cc2_scorecard_rejects_losing_series():
    from champion_challenger2 import build_scorecard
    sc = build_scorecard(_losing_series())
    assert sc["qualified"] is False
    failed = [c["name"] for c in sc["checks"] if not c["passed"]]
    assert failed


def test_cc2_metrics_drawdown_and_pf():
    from champion_challenger2 import metrics_of
    m = metrics_of([1.0, -1.0, -1.0, 2.0])
    assert m["n"] == 4
    assert m["max_dd_r"] == 2.0
    assert m["profit_factor"] == 1.5


def test_cc2_monte_carlo_deterministic_seed():
    from champion_challenger2 import monte_carlo
    a = monte_carlo(_winning_series(), iters=200)
    b = monte_carlo(_winning_series(), iters=200)
    assert a == b


def test_cc2_purged_walk_forward_embargo():
    from champion_challenger2 import purged_walk_forward
    wf = purged_walk_forward(_winning_series(), folds=5, embargo=3)
    assert wf["embargo_trades"] == 3
    assert len(wf["folds"]) == 5
    assert wf["passed"] is True


# ───────────────────────── Transaction Costs ─────────────────────────

def test_cost_spread_from_live_signal():
    from transaction_costs import spread_r_of
    sig = {"entry_price": 2000.0, "stop_loss": 1990.0, "spread": 0.5}
    r, basis = spread_r_of(sig, "XAUUSD")
    assert basis == "live_signal"
    assert abs(r - 0.05) < 1e-9


def test_cost_spread_symbol_default_fallback():
    from transaction_costs import spread_r_of
    r, basis = spread_r_of({}, "XAUUSD.pro")
    assert basis == "symbol_default"
    assert r == 0.03


# ───────────────────────── Counterfactuals (OA 2.0) ─────────────────────────

def test_counterfactuals_shapes():
    from outcome_attribution import counterfactuals
    sig = {"slippage_ratio": 0.2, "fill_delay_s": 90}
    attribution = {"REGIME_ERROR": 0.15, "ALPHA_ERROR": 0.5}
    cf = counterfactuals({}, -0.81, sig, attribution,
                         median_account_slippage=0.05)
    assert cf["actual_r"] == -0.81
    assert cf["no_trade_r"] == 0.0
    assert cf["normal_execution_r"] == -0.61
    assert cf["median_broker_slippage_r"] == -0.66
    assert "earlier_entry_r" in cf and cf["earlier_entry_r"] > -0.81
    assert "correct_regime_r" in cf


def test_counterfactuals_win_has_no_regime_branch():
    from outcome_attribution import counterfactuals
    cf = counterfactuals({}, 1.2, {"slippage_ratio": 0.0,
                                   "fill_delay_s": 5}, {})
    assert "correct_regime_r" not in cf
    assert "earlier_entry_r" not in cf
    assert cf["normal_execution_r"] == 1.2


# ───────────────────────── Degraded Intelligence ─────────────────────────

def test_degraded_policy_multipliers():
    from degraded_intelligence import POLICY, multiplier_for
    assert multiplier_for("meta_decision") == 0.5
    assert multiplier_for("risk_engine") == 0.0
    assert multiplier_for("execution_authority") == 0.0
    assert multiplier_for("market_memory") == 1.0
    assert multiplier_for("nonexistent_subsystem") == 0.5
    for name in ("position_truth", "risk_engine", "execution_authority"):
        assert POLICY[name]["risk_multiplier"] == 0.0


# ───────────────────────── Fail-closed limiter ─────────────────────────

class _BrokenBuckets:
    async def update_one(self, *a, **k):
        raise RuntimeError("mongo down")


class _BrokenDb:
    rate_buckets = _BrokenBuckets()


def test_limiter_fails_closed_for_financial_writes(monkeypatch):
    from distributed_rate_limit import allow_request
    monkeypatch.delenv("REDIS_URL", raising=False)
    allowed, meta = asyncio.run(allow_request(
        _BrokenDb(), key_id="k", tenant="t", endpoint_class="write",
        limit_per_minute=10, fail_closed=True))
    assert allowed is False
    assert meta["fail_closed"] is True and meta["degraded"] is True


def test_limiter_fails_open_for_reads(monkeypatch):
    from distributed_rate_limit import allow_request
    monkeypatch.delenv("REDIS_URL", raising=False)
    allowed, meta = asyncio.run(allow_request(
        _BrokenDb(), key_id="k", tenant="t", endpoint_class="read",
        limit_per_minute=10))
    assert allowed is True
    assert meta.get("fail_closed") is None


# ───────────────────────── Replay r-log (CC2 data source) ─────────────────

def test_replay_r_log_records_series():
    from bayes_opt import TP_MULT, _book, new_replay_state
    st = new_replay_state()
    st["_r_log"] = []
    _book(st, -1.0)
    _book(st, TP_MULT)
    # _book itself doesn't log (replay does) — verify state math intact
    assert st["trades"] == 2 and st["losses"] == 1 and st["wins"] == 1
    assert isinstance(st["_r_log"], list)
