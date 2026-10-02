"""Quant validation framework — pure numpy, no I/O.

Every optimiser / auto-promotion path in the bot (bayes_opt, nightly_tuner,
strategy_optimizer, ml_ensemble, learning_pipeline, online_learning) uses
these primitives so a "better" candidate has to survive the same honest
checks the scalp model already applies (scalp/model.py):

  purged_kfold / combinatorial_purged_cv
      Cross-validation splits for samples whose LABELS span a time window
      [t_open, t_close] (a trade's outcome is only known at its close).
      Training samples whose label window overlaps a test window are
      PURGED, and samples opening within `embargo` after a test window are
      EMBARGOED (López de Prado, AFML ch. 7 / 12).

  deflated_sharpe_ratio
      Probability that the true Sharpe is > 0 after correcting the selected
      strategy's Sharpe for (a) the number of trials it was the best of and
      (b) non-normal returns (Bailey & López de Prado, 2014).

  probability_of_backtest_overfitting
      CSCV estimate of the probability that the in-sample winner of a
      strategy family under-performs the family median out of sample
      (Bailey, Borwein, López de Prado & Zhu, 2015).

  cost_in_r / resolve_costs / DEFAULT_COSTS
      Round-trip trading cost expressed in R (multiples of the stop
      distance) so replays book net, not gross, R.

  block_bootstrap_lower_bound / bootstrap_auc_lower_bound
      One-sided lower confidence bounds that respect serial dependence
      (moving-block or stationary bootstrap, same idea as scalp/model.py).

  selection_gate
      The shared acceptance rule for auto-tuned candidates (OOS gain after
      costs, DSR, minimum OOS sample, PBO), configurable through env vars.

All functions are deterministic given their `seed` argument.
"""
from __future__ import annotations

import math
import os
from itertools import combinations
from statistics import NormalDist

import numpy as np

EULER_GAMMA = 0.5772156649015329
_N = NormalDist()

# ─────────────────────────────────────────────────────────────── splitters


def _as_times(t_open, t_close):
    to = np.asarray(t_open, dtype=float)
    tc = np.asarray(t_close, dtype=float)
    if to.shape != tc.shape or to.ndim != 1:
        raise ValueError("t_open and t_close must be 1-D arrays of equal length")
    if np.any(tc < to):
        raise ValueError("t_close must be >= t_open for every sample")
    return to, tc


def _contiguous_groups(order: np.ndarray, n_groups: int) -> list:
    """Split chronologically ordered indices into n_groups contiguous blocks."""
    return [np.sort(g) for g in np.array_split(order, n_groups) if len(g)]


def _purge_mask(to, tc, test_idx, embargo: float) -> np.ndarray:
    """Boolean mask of samples that may be used for TRAINING given one
    contiguous test block: drop label windows that overlap the block's span
    [min open, max close] and samples opening within the embargo after it."""
    lo = to[test_idx].min()
    hi = tc[test_idx].max()
    overlap = (to <= hi) & (tc >= lo)
    embargoed = (to > hi) & (to <= hi + embargo)
    return ~(overlap | embargoed)


def purged_kfold(t_open, t_close, n_splits: int = 5, embargo: float = 0.0):
    """Purged + embargoed K-fold.

    Samples are ordered by `t_open`; each fold's test set is a contiguous
    chronological block. Training = every other sample EXCEPT those whose
    label window [t_open, t_close] overlaps the test block's window, and
    those opening within `embargo` (same time unit as t_open) after the
    test block ends.

    Yields (train_idx, test_idx) as sorted int arrays of ORIGINAL indices.
    """
    to, tc = _as_times(t_open, t_close)
    n = len(to)
    if n_splits < 2 or n_splits > n:
        raise ValueError(f"n_splits must be in [2, n={n}]")
    order = np.argsort(to, kind="stable")
    for test in _contiguous_groups(order, n_splits):
        keep = _purge_mask(to, tc, test, float(embargo))
        keep[test] = False
        yield np.flatnonzero(keep), test


def cpcv_n_paths(n_groups: int, k_test: int) -> int:
    """Number of backtest paths CPCV produces: C(N-1, k-1)."""
    return math.comb(n_groups - 1, k_test - 1)


def cpcv_n_splits(n_groups: int, k_test: int) -> int:
    """Number of train/test splits CPCV produces: C(N, k)."""
    return math.comb(n_groups, k_test)


def combinatorial_purged_cv(t_open, t_close, n_groups: int = 6,
                            k_test: int = 2, embargo: float = 0.0):
    """Combinatorial purged CV (AFML ch. 12).

    The chronologically ordered samples are cut into `n_groups` contiguous
    groups; every combination of `k_test` groups is a test set (C(N, k)
    splits, C(N-1, k-1) full backtest paths). Each test group is purged and
    embargoed independently, so non-adjacent test groups do not purge the
    training data between them.

    Yields (train_idx, test_idx, test_group_ids).
    """
    to, tc = _as_times(t_open, t_close)
    n = len(to)
    if not (1 <= k_test < n_groups <= n):
        raise ValueError("need 1 <= k_test < n_groups <= n_samples")
    order = np.argsort(to, kind="stable")
    groups = _contiguous_groups(order, n_groups)
    for combo in combinations(range(len(groups)), k_test):
        keep = np.ones(n, dtype=bool)
        test_parts = []
        for g in combo:
            keep &= _purge_mask(to, tc, groups[g], float(embargo))
            test_parts.append(groups[g])
        test = np.sort(np.concatenate(test_parts))
        keep[test] = False
        yield np.flatnonzero(keep), test, combo


# ─────────────────────────────────────────────────────── moments / Sharpe


def sharpe(returns) -> float | None:
    """Per-observation (non-annualised) Sharpe; None if undefined."""
    r = np.asarray(returns, dtype=float)
    if r.size < 2:
        return None
    sd = r.std(ddof=1)
    if not np.isfinite(sd) or sd <= 1e-12:
        return None
    return float(r.mean() / sd)


def moments(returns) -> tuple[float, float]:
    """(skewness, kurtosis) — kurtosis is NON-excess (normal = 3)."""
    r = np.asarray(returns, dtype=float)
    if r.size < 3:
        return 0.0, 3.0
    m = r.mean()
    sd = r.std(ddof=0)
    if sd <= 1e-12:
        return 0.0, 3.0
    z = (r - m) / sd
    return float((z ** 3).mean()), float((z ** 4).mean())


def expected_max_sharpe(n_trials: int, sr_var_trials: float) -> float:
    """E[max SR] of n_trials unskilled strategies (False Strategy Theorem):
    sqrt(V) · ((1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e)))."""
    if n_trials <= 1 or sr_var_trials <= 0:
        return 0.0
    a = _N.inv_cdf(1.0 - 1.0 / n_trials)
    b = _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sr_var_trials) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b)


def probabilistic_sharpe_ratio(sr: float, sr_benchmark: float, n_obs: int,
                               skew: float = 0.0, kurt: float = 3.0) -> float:
    """PSR: P[true SR > sr_benchmark] given an observed per-obs SR."""
    if n_obs < 2:
        return 0.0
    denom = 1.0 - skew * sr + (kurt - 1.0) / 4.0 * sr * sr
    if denom <= 0:
        denom = 1e-12
    z = (sr - sr_benchmark) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return float(_N.cdf(z))


def deflated_sharpe_ratio(sr: float, n_trials: int, n_obs: int,
                          skew: float = 0.0, kurt: float = 3.0,
                          sr_var_trials: float = 0.0) -> float:
    """Deflated Sharpe Ratio (Bailey & López de Prado, 2014).

    sr            observed per-observation Sharpe of the SELECTED strategy
    n_trials      number of (effectively independent) configurations tried
    n_obs         number of return observations behind `sr`
    skew, kurt    skewness and NON-excess kurtosis of those returns
    sr_var_trials cross-sectional variance of the trials' Sharpe ratios

    DSR = PSR(SR₀) with SR₀ = expected_max_sharpe(n_trials, sr_var_trials).
    Values ≥ 0.95 mean the Sharpe is significant at 5% after selection bias.
    """
    sr0 = expected_max_sharpe(int(n_trials), float(sr_var_trials))
    return probabilistic_sharpe_ratio(float(sr), sr0, int(n_obs),
                                      float(skew), float(kurt))


def dsr_from_trials(selected_returns, trial_sharpes, n_trials: int | None = None,
                    sr_var_override: float | None = None) -> dict:
    """Convenience wrapper: DSR for the selected strategy's per-trade returns
    given the Sharpe of every trial (None entries ignored for the variance,
    but still counted in n_trials)."""
    r = np.asarray(selected_returns, dtype=float)
    srs = [s for s in trial_sharpes if s is not None and np.isfinite(s)]
    n_tr = int(n_trials if n_trials is not None else len(trial_sharpes))
    var = (float(sr_var_override) if sr_var_override is not None
           else (float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.0))
    sr = sharpe(r)
    if sr is None:
        return {"dsr": None, "sr": None, "n_obs": int(r.size),
                "n_trials": n_tr, "sr_var_trials": round(var, 6),
                "reason": "selected strategy has <2 trades or zero variance"}
    sk, ku = moments(r)
    dsr = deflated_sharpe_ratio(sr, n_tr, int(r.size), sk, ku, var)
    return {"dsr": round(dsr, 4), "sr": round(sr, 4), "n_obs": int(r.size),
            "n_trials": n_tr, "sr_var_trials": round(var, 6),
            "sr0": round(expected_max_sharpe(n_tr, var), 4),
            "skew": round(sk, 3), "kurt": round(ku, 3)}


# ─────────────────────────────────────────────────────────────────── PBO


def _avg_rank(x: np.ndarray) -> np.ndarray:
    """1-based ascending ranks with ties averaged."""
    order = np.argsort(x, kind="stable")
    ranks = np.empty(len(x), dtype=float)
    ranks[order] = np.arange(1, len(x) + 1, dtype=float)
    for v in np.unique(x):
        m = x == v
        if m.sum() > 1:
            ranks[m] = ranks[m].mean()
    return ranks


def _perf(block: np.ndarray, metric: str) -> np.ndarray:
    mu = block.mean(axis=0)
    if metric == "mean":
        return mu
    sd = block.std(axis=0, ddof=1) if block.shape[0] > 1 else np.zeros_like(mu)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(sd > 1e-12, mu / np.where(sd > 1e-12, sd, 1.0), 0.0)
    return out


def probability_of_backtest_overfitting(performance_matrix, n_blocks: int = 8,
                                        metric: str = "sharpe") -> dict:
    """PBO via Combinatorially Symmetric Cross-Validation.

    performance_matrix  T×N array: T time-ordered observations (e.g. per
                        period P&L) for N strategy configurations (trials).
    n_blocks            S (even): rows are cut into S contiguous blocks; every
                        C(S, S/2) half/half combination is an IS/OOS split.
    metric              "sharpe" (default, as in the paper) or "mean".

    For each split the IS-best configuration's OOS relative rank ω ∈ (0,1)
    gives a logit λ = ln(ω/(1-ω)); PBO = P[λ ≤ 0], i.e. the IS winner lands
    at or below the OOS median. ~0.5 for a family of pure-noise strategies,
    → 1 when IS winners systematically mean-revert, → 0 for a genuinely
    dominant configuration.
    """
    M = np.asarray(performance_matrix, dtype=float)
    if M.ndim != 2 or M.shape[1] < 2:
        raise ValueError("performance_matrix must be T×N with N ≥ 2")
    S = int(n_blocks)
    if S < 2 or S % 2:
        raise ValueError("n_blocks must be an even number ≥ 2")
    if M.shape[0] < S:
        raise ValueError(f"need at least n_blocks={S} rows, got {M.shape[0]}")
    blocks = np.array_split(np.arange(M.shape[0]), S)
    N = M.shape[1]
    logits = []
    for combo in combinations(range(S), S // 2):
        is_rows = np.concatenate([blocks[i] for i in combo])
        oos_rows = np.concatenate([blocks[i] for i in range(S) if i not in combo])
        r_is = _perf(M[is_rows], metric)
        r_oos = _perf(M[oos_rows], metric)
        best = int(np.argmax(r_is))
        w = _avg_rank(r_oos)[best] / (N + 1.0)
        logits.append(math.log(w / (1.0 - w)))
    lg = np.asarray(logits)
    return {"pbo": round(float((lg <= 0).mean()), 4),
            "n_splits": int(len(lg)), "n_trials": int(N),
            "median_logit": round(float(np.median(lg)), 4)}


# ──────────────────────────────────────────────────────────────── costs

# Conservative ROUND-TRIP cost assumptions in PRICE units (spread at
# entry, combined slippage of both fills, commission converted to price via
# the contract size). Override per call with `costs=` or via env
# TUNER_COSTS_JSON='{"XAUUSD": {"spread": 0.3, ...}}'.
DEFAULT_COSTS = {
    "XAUUSD": {"spread": 0.30, "slippage": 0.10, "commission": 0.07},
    "XAGUSD": {"spread": 0.03, "slippage": 0.01, "commission": 0.0},
    "BTCUSD": {"spread": 20.0, "slippage": 8.0, "commission": 0.0},
    "ETHUSD": {"spread": 1.5, "slippage": 0.6, "commission": 0.0},
    "EURUSD": {"spread": 0.00010, "slippage": 0.00004, "commission": 0.00007},
    "GBPUSD": {"spread": 0.00014, "slippage": 0.00005, "commission": 0.00007},
    "USDJPY": {"spread": 0.012, "slippage": 0.005, "commission": 0.007},
    "US30": {"spread": 2.5, "slippage": 1.0, "commission": 0.0},
    "NAS100": {"spread": 1.5, "slippage": 0.6, "commission": 0.0},
}
# Floor in R applied to every trade (matches transaction_costs.COST_FLOOR_R
# + a little) — used alone when the symbol's price costs are unknown.
COST_FLOOR_R = 0.05


def cost_in_r(spread: float, slippage: float, commission: float,
              sl_distance: float) -> float:
    """Round-trip cost of one trade in R = (spread + slippage + commission)
    / stop distance. All four arguments in the SAME price units."""
    sl = float(sl_distance)
    if not np.isfinite(sl) or sl <= 0:
        raise ValueError("sl_distance must be > 0")
    tot = float(spread or 0) + float(slippage or 0) + float(commission or 0)
    if tot < 0:
        raise ValueError("costs must be non-negative")
    return tot / sl


def resolve_costs(symbol: str | None = None, costs: dict | None = None) -> dict:
    """Cost spec for a symbol: explicit `costs` > env TUNER_COSTS_JSON >
    DEFAULT_COSTS > R floor only. Always carries `floor_r`."""
    if costs:
        out = dict(costs)
    else:
        base = str(symbol or "").upper()[:6]
        table = dict(DEFAULT_COSTS)
        raw = os.environ.get("TUNER_COSTS_JSON")
        if raw:
            try:
                import json
                table.update({k.upper(): v for k, v in json.loads(raw).items()})
            except (ValueError, AttributeError):
                pass
        out = dict(table.get(base) or {})
        out["source"] = "default_table" if out else "floor_only"
    out.setdefault("floor_r", COST_FLOOR_R)
    return out


def trade_cost_r(costs: dict | None, sl_distance: float) -> float:
    """Cost in R of one trade under a `resolve_costs` spec (0 if None)."""
    if not costs:
        return 0.0
    if costs.get("cost_r") is not None:
        return max(float(costs["cost_r"]), 0.0)
    floor = float(costs.get("floor_r") or 0.0)
    if not any(costs.get(k) for k in ("spread", "slippage", "commission")):
        return floor
    try:
        c = cost_in_r(costs.get("spread", 0), costs.get("slippage", 0),
                      costs.get("commission", 0), sl_distance)
    except ValueError:
        return floor
    return max(c, floor)


# ─────────────────────────────────────────────────────────── bootstrap


def bootstrap_indices(n: int, iters: int = 1000, block: int | None = None,
                      seed: int = 42, stationary: bool = False) -> np.ndarray:
    """iters×n resample index matrix that keeps serial dependence.

    Moving-block (fixed length `block`) by default; `stationary=True` gives
    the Politis–Romano stationary bootstrap (geometric block lengths with
    mean `block`, wrapping around)."""
    if n < 1:
        raise ValueError("n must be ≥ 1")
    block = int(block or min(10, max(1, n // 5)))
    block = max(1, min(block, n))
    rng = np.random.default_rng(seed)
    if not stationary:
        n_blocks = int(math.ceil(n / block))
        starts = rng.integers(0, n - block + 1, size=(iters, n_blocks))
        return (starts[:, :, None] + np.arange(block)[None, None, :]
                ).reshape(iters, -1)[:, :n]
    p = 1.0 / block
    out = np.empty((iters, n), dtype=int)
    for i in range(iters):
        idx = int(rng.integers(0, n))
        for j in range(n):
            if j and rng.random() < p:
                idx = int(rng.integers(0, n))
            elif j:
                idx = (idx + 1) % n
            out[i, j] = idx
    return out


def block_bootstrap_lower_bound(values, alpha: float = 0.05,
                                iters: int = 1000, block: int | None = None,
                                seed: int = 42, stationary: bool = False,
                                stat=np.mean) -> float:
    """One-sided (1-alpha) lower bound of `stat(values)` under a block
    bootstrap (same construction as scalp/model._lower_bound)."""
    v = np.asarray(values, dtype=float)
    if v.size < 2:
        return float("-inf")
    idx = bootstrap_indices(v.size, iters, block, seed, stationary)
    stats = np.array([stat(v[row]) for row in idx])
    return float(np.quantile(stats, alpha))


def auc(y, p) -> float | None:
    """ROC AUC via Mann-Whitney ranks (ties averaged); None if one class."""
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=float)
    n1 = int((y == 1).sum())
    n0 = int((y == 0).sum())
    if n1 == 0 or n0 == 0:
        return None
    r = _avg_rank(p)
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))


def bootstrap_auc_lower_bound(y, p, alpha: float = 0.05, iters: int = 1000,
                              block: int | None = None, seed: int = 42) -> float | None:
    """One-sided lower bound of AUC with paired block resampling of (y, p)
    in time order. Resamples with a single class are skipped."""
    y = np.asarray(y, dtype=int)
    p = np.asarray(p, dtype=float)
    if len(y) < 4 or auc(y, p) is None:
        return None
    vals = []
    for row in bootstrap_indices(len(y), iters, block, seed):
        a = auc(y[row], p[row])
        if a is not None:
            vals.append(a)
    if len(vals) < max(20, iters // 4):
        return None
    return float(np.quantile(vals, alpha))


def multiple_testing_alpha(n_tests: int, alpha: float = 0.05,
                           min_alpha: float = 0.005) -> float:
    """Bonferroni-adjusted one-sided alpha for the n-th repeated test,
    floored so the bootstrap quantile stays estimable."""
    return max(alpha / max(1, int(n_tests)), min_alpha)


# ──────────────────────────────────────────────────────── acceptance gate


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def gate_thresholds() -> dict:
    """Env-configurable acceptance thresholds (read at call time)."""
    return {"min_dsr": _env_float("TUNER_MIN_DSR", 0.95),
            "min_oos_trades": int(_env_float("TUNER_MIN_OOS_TRADES", 20)),
            "max_pbo": _env_float("TUNER_MAX_PBO", 0.2),
            "pbo_min_trials": int(_env_float("TUNER_PBO_MIN_TRIALS", 10))}


def selection_gate(*, oos_improvement: float | None, oos_trades: int | None,
                   dsr: float | None, pbo: float | None = None,
                   n_trials: int = 0, thresholds: dict | None = None) -> dict:
    """Shared acceptance rule for an auto-selected candidate. ALL of:
      · out-of-sample improvement vs baseline > 0 after costs
      · ≥ min_oos_trades out-of-sample trades for the candidate
      · DSR ≥ min_dsr (Sharpe significant after selection bias)
      · PBO ≤ max_pbo — only enforced when n_trials ≥ pbo_min_trials
        (with too few trials CSCV is uninformative; PBO=None then passes)."""
    th = {**gate_thresholds(), **(thresholds or {})}
    pbo_required = n_trials >= th["pbo_min_trials"]
    checks = [
        {"name": "oos_improvement_after_costs > 0", "value": oos_improvement,
         "passed": oos_improvement is not None and oos_improvement > 0},
        {"name": f"oos_trades ≥ {th['min_oos_trades']}", "value": oos_trades,
         "passed": (oos_trades or 0) >= th["min_oos_trades"]},
        {"name": f"deflated_sharpe ≥ {th['min_dsr']}", "value": dsr,
         "passed": dsr is not None and dsr >= th["min_dsr"]},
        {"name": f"pbo ≤ {th['max_pbo']}"
                 + ("" if pbo_required else " (not enforced: too few trials)"),
         "value": pbo,
         "passed": (not pbo_required) or (pbo is not None and pbo <= th["max_pbo"])},
    ]
    failed = [c["name"] for c in checks if not c["passed"]]
    return {"accepted": not failed, "checks": checks, "failed": failed,
            "thresholds": th}
