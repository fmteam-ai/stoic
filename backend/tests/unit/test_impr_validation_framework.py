"""Quant validation framework (validation.py) + its wiring into the tuners,
shadow lab, ML ensemble, learning pipeline and online learning."""
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import validation as V  # noqa: E402

pytestmark = pytest.mark.unit


def _windows(n=120, seed=0):
    rng = np.random.default_rng(seed)
    t_open = np.cumsum(rng.uniform(1, 10, n))
    t_close = t_open + rng.uniform(0, 40, n)          # overlapping labels
    return t_open, t_close


# ─────────────────────────────────────────────────────────── purged k-fold

@pytest.mark.parametrize("embargo", [0.0, 15.0])
def test_purged_kfold_no_overlap_and_embargo(embargo):
    to, tc = _windows()
    seen = []
    for train, test in V.purged_kfold(to, tc, n_splits=5, embargo=embargo):
        assert len(np.intersect1d(train, test)) == 0
        lo, hi = to[test].min(), tc[test].max()
        # no training label window overlaps any test label window
        for i in train:
            assert tc[i] < lo or to[i] > hi
            # embargo: nothing opens within `embargo` after the test block
            assert not (hi < to[i] <= hi + embargo)
        for j in test:
            assert not np.any((to[train] <= tc[j]) & (tc[train] >= to[j]))
        seen.extend(test.tolist())
    assert sorted(seen) == list(range(len(to)))        # tests partition data


def test_purged_kfold_embargo_removes_more():
    to, tc = _windows()
    n0 = sum(len(tr) for tr, _ in V.purged_kfold(to, tc, 4, 0.0))
    n1 = sum(len(tr) for tr, _ in V.purged_kfold(to, tc, 4, 50.0))
    assert n1 < n0


def test_purged_kfold_rejects_bad_input():
    with pytest.raises(ValueError):
        list(V.purged_kfold([0, 1], [1, 0], 2))
    with pytest.raises(ValueError):
        list(V.purged_kfold([0, 1, 2], [1, 2, 3], 1))


# ───────────────────────────────────────────────────────────────── CPCV

def test_cpcv_split_and_path_counts():
    to, tc = _windows(90)
    splits = list(V.combinatorial_purged_cv(to, tc, n_groups=6, k_test=2,
                                            embargo=5.0))
    assert len(splits) == V.cpcv_n_splits(6, 2) == 15
    assert V.cpcv_n_paths(6, 2) == 5
    # each group is a test group in exactly C(N-1, k-1) splits = #paths
    counts = np.zeros(6, int)
    for train, test, combo in splits:
        for g in combo:
            counts[g] += 1
        assert len(np.intersect1d(train, test)) == 0
        for j in test:
            assert not np.any((to[train] <= tc[j]) & (tc[train] >= to[j]))
    assert counts.tolist() == [5] * 6
    assert V.cpcv_n_paths(10, 3) == math.comb(9, 2)


# ────────────────────────────────────────────────────────────────── DSR

def test_dsr_matches_bailey_lopez_de_prado_example():
    # Bailey & López de Prado (2014) numerical example: annualised SR 2.5,
    # T=1250 daily obs, N=100 trials, V[SR]=0.5 (annualised), skew −3,
    # kurtosis 10  →  DSR ≈ 0.9004.
    sr = 2.5 / math.sqrt(250)
    dsr = V.deflated_sharpe_ratio(sr, n_trials=100, n_obs=1250, skew=-3,
                                  kurt=10, sr_var_trials=0.5 / 250)
    assert dsr == pytest.approx(0.9004, abs=5e-4)


def test_dsr_hand_computed_simple_case():
    # Hand computation: N=10, V=0.01 → SR0 = 0.1·((1−γ)·Φ⁻¹(0.9)
    # + γ·Φ⁻¹(1−1/(10e))) = 0.1·(0.422784·1.281552 + 0.577216·1.789242)
    # = 0.1·(0.541820 + 1.032778) = 0.157460; normal returns (skew 0,
    # kurt 3), SR=0.3, T=101: z = (0.3−0.157460)·10 / sqrt(1 + 0.5·0.09)
    # = 1.425402 / 1.022252 = 1.394374 → Φ(z) = 0.918398
    assert V.expected_max_sharpe(10, 0.01) == pytest.approx(0.157460, abs=2e-6)
    assert V.deflated_sharpe_ratio(0.3, 10, 101, 0.0, 3.0, 0.01) == \
        pytest.approx(0.918398, abs=2e-6)
    # one trial / no dispersion → plain PSR against 0
    assert V.deflated_sharpe_ratio(0.3, 1, 101) == pytest.approx(
        V.probabilistic_sharpe_ratio(0.3, 0.0, 101))


def test_dsr_falls_with_more_trials():
    a = V.deflated_sharpe_ratio(0.25, 5, 60, 0, 3, 0.02)
    b = V.deflated_sharpe_ratio(0.25, 500, 60, 0, 3, 0.02)
    assert b < a


# ────────────────────────────────────────────────────────────────── PBO

def test_pbo_iid_noise_is_not_low():
    # For an unskilled family the IS winner's OOS rank is uniform → PBO ≈ 0.5
    # on average (single draws vary a lot, so average over seeds).
    pbos = []
    for seed in range(10):
        M = np.random.default_rng(seed).normal(0, 1, (240, 30))
        out = V.probability_of_backtest_overfitting(M, n_blocks=8)
        assert out["n_splits"] == math.comb(8, 4)
        pbos.append(out["pbo"])
    assert 0.35 < float(np.mean(pbos)) < 0.65
    assert min(pbos) > 0.2                   # never looks "safe"


def test_pbo_near_one_for_overfit_noise():
    # Pure noise demeaned per strategy (zero true AND zero sample edge):
    # whatever looks best in-sample is by construction worst out-of-sample.
    rng = np.random.default_rng(2)
    M = rng.normal(0, 1, (160, 20))
    M -= M.mean(axis=0)
    out = V.probability_of_backtest_overfitting(M, n_blocks=8, metric="mean")
    assert out["pbo"] > 0.95


def test_pbo_low_for_genuinely_better_strategy():
    rng = np.random.default_rng(3)
    M = rng.normal(0, 1, (240, 20))
    M[:, 7] += 0.8                            # real, persistent edge
    out = V.probability_of_backtest_overfitting(M, n_blocks=8)
    assert out["pbo"] < 0.05


# ──────────────────────────────────────────────────────────────── costs

def test_cost_in_r():
    assert V.cost_in_r(0.30, 0.10, 0.07, 3.0) == pytest.approx(0.47 / 3.0)
    assert V.cost_in_r(0, 0, 0, 1.0) == 0.0
    with pytest.raises(ValueError):
        V.cost_in_r(0.3, 0.1, 0.0, 0.0)
    with pytest.raises(ValueError):
        V.cost_in_r(-1.0, 0.0, 0.0, 1.0)


def test_trade_cost_r_floor_and_resolution():
    spec = V.resolve_costs("XAUUSD-ECN")
    assert spec["spread"] == V.DEFAULT_COSTS["XAUUSD"]["spread"]
    # tiny stop → big cost; huge stop → floored
    assert V.trade_cost_r(spec, 1.0) == pytest.approx(0.47)
    assert V.trade_cost_r(spec, 1000.0) == V.COST_FLOOR_R
    assert V.trade_cost_r(V.resolve_costs("UNKNOWN"), 5.0) == V.COST_FLOOR_R
    assert V.trade_cost_r({"cost_r": 0.12}, 5.0) == 0.12
    assert V.trade_cost_r(None, 5.0) == 0.0


# ──────────────────────────────────────────────────────────── bootstrap

def test_block_bootstrap_lower_bound_and_auc():
    rng = np.random.default_rng(4)
    v = rng.normal(0.2, 1.0, 300)
    lb = V.block_bootstrap_lower_bound(v, alpha=0.05)
    assert lb < v.mean()
    lb_s = V.block_bootstrap_lower_bound(v, alpha=0.05, stationary=True,
                                         iters=200)
    assert lb_s < v.mean()
    from sklearn.metrics import roc_auc_score
    y = rng.integers(0, 2, 200)
    p = np.round(rng.random(200) + 0.3 * y, 1)        # ties included
    assert V.auc(y, p) == pytest.approx(roc_auc_score(y, p))
    lb_auc = V.bootstrap_auc_lower_bound(y, p, alpha=0.05)
    assert lb_auc < V.auc(y, p)
    assert V.multiple_testing_alpha(1) == 0.05
    assert V.multiple_testing_alpha(5) == pytest.approx(0.01)
    assert V.multiple_testing_alpha(1000) == 0.005


# ─────────────────────────────────────────────────────────────── bayes_opt

def _bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1}


def _synthetic_bars(n=320, seed=5):
    rng = np.random.default_rng(seed)
    px, bars = 2000.0, []
    for i in range(n):
        o = px
        px += float(rng.normal(0.5, 4))
        bars.append(_bar(900 * i, o, round(max(o, px) + 3, 2),
                         round(min(o, px) - 3, 2), round(px, 2)))
    return bars


def test_bayes_replay_books_cost_net_r():
    import bayes_opt as B
    st = B.new_replay_state()
    st["open_pos"] = {"action": "BUY", "entry": 100.0, "sl": 99.0,
                      "tp": 102.0, "opened_t": 0, "cost_r": 0.25}
    bars = [_bar(900 * i, 100, 100.5, 99.5, 100) for i in range(B.WARMUP + 1)]
    bars.append(_bar(900 * (B.WARMUP + 1), 100, 102.5, 99.5, 102))
    out = B.replay("breakout_m15", bars, [None] * len(bars), None,
                   start=len(bars) - 1, state=st)
    assert out["total_r"] == pytest.approx(B.TP_MULT - 0.25)
    assert out["cost_r"] == pytest.approx(0.25)


def test_bayes_opt_oos_split_and_costs(monkeypatch):
    import bayes_opt as B
    bars = _synthetic_bars()
    calls = []
    real = B.replay

    def spy(engine, b, f, params, start=None, state=None, *, costs=None):
        calls.append({"n": len(b), "start": start, "costs": costs})
        return real(engine, b, f, params, start=start, state=state,
                    costs=costs)
    monkeypatch.setattr(B, "replay", spy)
    res = B.optimize_engine_params("breakout_m15", bars, iters=4, init=3,
                                   costs={"cost_r": 0.1})
    is_end, oos_start = B.split_bars(len(bars))
    assert res["split"]["is_bars"] == is_end == int(len(bars) * B.IS_FRAC)
    assert res["split"]["oos_start"] == oos_start > is_end
    # the optimiser (8 probes) only ever saw the in-sample bars
    probes = calls[:res["evaluations"]]
    assert all(c["n"] == is_end and c["start"] is None for c in probes)
    # the two OOS replays start after the embargo on untouched bars
    oos_calls = calls[res["evaluations"]:]
    assert len(oos_calls) == 2
    assert all(c["start"] == oos_start for c in oos_calls)
    assert all(c["costs"]["cost_r"] == 0.1 for c in calls)
    # every trial reported, costs charged per trade
    assert len(res["trials"]) == res["evaluations"] == 8
    for t in res["trials"]:
        assert t["cost_r"] == pytest.approx(0.1 * t["trades"], abs=0.02)
    assert res["oos"]["best"] is not None and res["oos"]["default"] is not None
    assert res["oos_improvement"] == pytest.approx(
        res["oos"]["best"]["total_r"] - res["oos"]["default"]["total_r"],
        abs=1e-3)
    assert res["dsr"]["n_trials"] == 8
    assert "pbo" in res["pbo"]
    assert res["best"]["score"] >= res["default"]["score"]   # IS (legacy)


def test_bayes_opt_defaults_to_symbol_cost_model():
    import bayes_opt as B
    res = B.optimize_engine_params("breakout_m15", _synthetic_bars(), iters=2,
                                   init=2, symbol="XAUUSD")
    assert res["costs"]["spread"] == V.DEFAULT_COSTS["XAUUSD"]["spread"]


# ─────────────────────────────────────────────────────────── tuner gate

def _prop(**kw):
    p = {"improvement": 6.0, "best": {"trades": 40},
         "oos": {"best": {"trades": 30}}, "oos_improvement": 3.0,
         "dsr": {"dsr": 0.97}, "pbo": {"pbo": 0.1},
         "trial_log": {"n_trials_total": 31}, "evaluations": 31}
    p.update(kw)
    return p


def test_tuner_gate_rejects_in_sample_only_gain(monkeypatch):
    from nightly_tuner import gate_proposal
    for k in ("TUNER_MIN_DSR", "TUNER_MIN_OOS_TRADES", "TUNER_MAX_PBO"):
        monkeypatch.delenv(k, raising=False)
    assert gate_proposal(_prop())["accepted"] is True
    # big in-sample gain, but it evaporates out of sample
    g = gate_proposal(_prop(improvement=25.0, oos_improvement=-1.5))
    assert not g["accepted"]
    assert any("oos_improvement" in f for f in g["failed"])
    # legacy (pre-validation) proposal with no OOS evidence at all
    assert not gate_proposal(_prop(oos_improvement=None, oos={}, dsr={},
                                   pbo={}))["accepted"]
    # OOS positive but selection-bias not survived
    assert not gate_proposal(_prop(dsr={"dsr": 0.6}))["accepted"]
    assert not gate_proposal(_prop(pbo={"pbo": 0.45}))["accepted"]
    assert not gate_proposal(_prop(oos={"best": {"trades": 12}}))["accepted"]
    # PBO is not enforced with too few trials
    assert gate_proposal(_prop(pbo={"pbo": None},
                               trial_log={"n_trials_total": 4}))["accepted"]


def test_tuner_gate_env_knobs(monkeypatch):
    from nightly_tuner import gate_proposal
    monkeypatch.setenv("TUNER_MIN_DSR", "0.99")
    assert not gate_proposal(_prop())["accepted"]
    monkeypatch.setenv("TUNER_MIN_DSR", "0.5")
    monkeypatch.setenv("TUNER_MIN_OOS_TRADES", "50")
    assert not gate_proposal(_prop())["accepted"]


def test_genetics_label_is_honest():
    from strategy_genetics import tuning_why
    assert "no out-of-sample" in tuning_why({"improvement": 3.0})
    txt = tuning_why({"improvement": 3.0, "oos_improvement": -0.5,
                      "dsr": {"dsr": 0.4}, "pbo": {"pbo": 0.6},
                      "gate": {"accepted": False, "failed": ["x"]}})
    assert "walk-forward" not in txt and "OOS" in txt and "gate failed" in txt


# ─────────────────────────────────────────────────────── strategy optimizer

def _trade(day, hour, pnl, sym="XAUUSD", now=None):
    now = now or datetime(2026, 9, 30, tzinfo=timezone.utc)
    c = (now - timedelta(days=day)).replace(hour=hour, minute=30)
    return {"symbol": sym, "pnl": pnl,
            "opened_at": (c - timedelta(minutes=20)).isoformat(),
            "closed_at": c.isoformat()}


def test_strategy_optimizer_rejects_in_sample_only_winner():
    from strategy_optimizer import validate_selection
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    rng = np.random.default_rng(9)
    trades = []
    for d in range(1, 75):
        # tokyo: great in-sample, losing out-of-sample (last 18 days)
        tokyo_pnl = 30.0 if d > 18 else (10.0 if d % 2 else -40.0)
        trades.append(_trade(d, 2, tokyo_pnl + float(rng.normal(0, 2)), now=now))
        trades.append(_trade(d, 13, float(rng.normal(1, 10)), now=now))
    v = validate_selection(trades, ["XAUUSD"], "any", now=now)
    assert v["selected"]["filters"]["session_preference"] == "tokyo"
    assert v["oos_improvement"] < 0
    assert v["accepted"] is False


# ─────────────────────────────────────────────────── ml ensemble / pipeline

def test_ml_cv_requires_25_per_fold():
    import ml_ensemble as me
    method, splits = me._cv_splits(40)
    assert splits == []                                    # 40 → insufficient
    to = np.arange(120) * 3600.0
    tc = to + 7200.0
    method, splits = me._cv_splits(120, list(to), list(tc))
    assert method == "purged_kfold" and len(splits) == 4
    for tr, va in splits:
        assert len(va) >= me.MIN_VAL_FOLD
        for j in va:
            assert not np.any((to[tr] <= tc[j]) & (tc[tr] >= to[j]))
    rng = np.random.default_rng(0)
    res = me.cv_skill_weights(rng.random((40, 4)), rng.integers(0, 2, 40))
    assert res["cv"]["status"] == "insufficient_validation_data"
    assert sum(res["weights"].values()) == 0.0


def test_pipeline_scores_with_deployed_weights():
    from learning_pipeline import _deployed_blend, purge_train

    class M:
        def __init__(self, p):
            self.p = p

        def predict_proba(self, X):
            return np.column_stack([1 - self.p, self.p])

    models = {"a": M(np.array([0.9, 0.1])), "b": M(np.array([0.1, 0.9]))}
    Xh = np.zeros((2, 1))
    eq = _deployed_blend(models, None, Xh)               # legacy equal
    assert eq.tolist() == pytest.approx([0.5, 0.5])
    w = _deployed_blend(models, {"a": 0.75, "b": 0.25}, Xh)
    assert w.tolist() == pytest.approx([0.7, 0.3])
    assert _deployed_blend(models, {"a": 0.0, "b": 0.0}, Xh) is None
    hold = [{"opened_at": "2026-01-02T00:00:00+00:00"}]
    train = [{"closed_at": "2026-01-01T00:00:00+00:00"},
             {"closed_at": "2026-01-02T05:00:00+00:00"}]   # straddles
    assert purge_train(train, hold) == train[:1]


# ─────────────────────────────────────────────────────────── online learning

def test_online_learning_schedule_and_test_count():
    from online_learning import recent_tests, should_retrain
    assert should_retrain(5, 10 * 86400) is None          # was a retrain
    assert should_retrain(19, 10 * 86400) is None
    assert should_retrain(20, 3600) is None               # too soon
    assert should_retrain(20, 25 * 3600) is not None
    assert should_retrain(3, 7200, min_new=3, min_interval_s=3600)
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    log = [(now - timedelta(days=d)).isoformat() for d in (1, 5, 29, 31, 60)]
    assert recent_tests(log, now=now) == 3
