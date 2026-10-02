"""Small Gaussian HMM for market regimes — pure NumPy, no new dependencies.

Observations are ATR-normalised bar returns
    x_t = (c_t − c_{t−1}) / ATR14_{t−1}
so the model is scale-free across symbols. K (2-3) hidden states with a
univariate Gaussian emission each; states are ordered by emission variance
(state 0 = calmest, state K−1 = most turbulent).

* Fitting: Baum-Welch EM with scaled forward-backward recursions. The
  transition matrix gets a **sticky Dirichlet prior** (Fox et al. 2011
  "sticky HDP-HMM" idea in its simplest MAP form): pseudo-counts
  `alpha` everywhere + `kappa` on the diagonal, so regimes persist instead
  of flickering bar-to-bar on noisy returns. Variances are floored.
* Inference: FORWARD FILTERING only — P(s_t | x_1..t). No smoothing at the
  last bar, so the live regime probability never looks ahead.

`regime_hmm_probabilities(bars)` is the convenience entry point used by
market_regime.regime_probabilities(); it returns None when there are too
few bars or the fit degenerates (callers fall back to their heuristic).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

HMM_MIN_BARS = 120
HMM_MAX_BARS = 600
VAR_FLOOR = 1e-4
STATE_LABELS = {2: ("calm", "turbulent"),
                3: ("calm", "normal", "turbulent")}


@dataclass
class HMMParams:
    pi: np.ndarray        # (K,)
    A: np.ndarray         # (K, K)
    mu: np.ndarray        # (K,)
    var: np.ndarray       # (K,)

    def to_dict(self) -> dict:
        return {"pi": self.pi.tolist(), "A": self.A.tolist(),
                "mu": self.mu.tolist(), "var": self.var.tolist()}

    @classmethod
    def from_dict(cls, d: dict) -> "HMMParams":
        return cls(np.asarray(d["pi"], float), np.asarray(d["A"], float),
                   np.asarray(d["mu"], float), np.asarray(d["var"], float))


def atr_normalised_returns(bars: list, period: int = 14) -> np.ndarray:
    """x_t = Δclose_t / ATR(period) at t−1 (no look-ahead)."""
    if not bars or len(bars) < period + 3:
        return np.array([])
    h = np.array([float(b["h"]) for b in bars])
    lo = np.array([float(b["l"]) for b in bars])
    c = np.array([float(b["c"]) for b in bars])
    tr = np.maximum.reduce([h[1:] - lo[1:], np.abs(h[1:] - c[:-1]),
                            np.abs(lo[1:] - c[:-1])])
    # trailing mean of TR ending at bar i (index into tr: i-1)
    csum = np.concatenate([[0.0], np.cumsum(tr)])
    out = []
    for i in range(period + 1, len(c)):
        # ATR over tr[i-1-period .. i-2] → bars up to i-1
        atr = (csum[i - 1] - csum[i - 1 - period]) / period
        if atr <= 0:
            continue
        out.append((c[i] - c[i - 1]) / atr)
    return np.asarray(out, dtype=float)


def _emission(x: np.ndarray, mu: np.ndarray, var: np.ndarray) -> np.ndarray:
    """(T, K) Gaussian likelihoods, floored to avoid underflow."""
    d = x[:, None] - mu[None, :]
    b = np.exp(-0.5 * d * d / var[None, :]) / np.sqrt(2 * math.pi * var[None, :])
    return np.maximum(b, 1e-300)


def forward_filter(x: np.ndarray, p: HMMParams):
    """Scaled forward pass. Returns (filtered (T,K), log-likelihood)."""
    T, K = len(x), len(p.pi)
    B = _emission(x, p.mu, p.var)
    alpha = np.empty((T, K))
    c = np.empty(T)
    a = p.pi * B[0]
    c[0] = a.sum()
    alpha[0] = a / c[0]
    At = p.A
    for t in range(1, T):
        a = (alpha[t - 1] @ At) * B[t]
        c[t] = a.sum()
        alpha[t] = a / c[t]
    return alpha, float(np.log(c).sum())


def _backward(x: np.ndarray, p: HMMParams, B: np.ndarray, c: np.ndarray):
    T, K = B.shape
    beta = np.empty((T, K))
    beta[-1] = 1.0
    for t in range(T - 2, -1, -1):
        beta[t] = (p.A @ (B[t + 1] * beta[t + 1])) / c[t + 1]
    return beta


def _init_params(x: np.ndarray, K: int) -> HMMParams:
    """Variance-quantile initialisation: split |x| into K buckets."""
    ax = np.abs(x - np.median(x))
    qs = np.quantile(ax, np.linspace(0, 1, K + 1))
    mu, var = np.zeros(K), np.zeros(K)
    for k in range(K):
        m = (ax >= qs[k]) & (ax <= qs[k + 1])
        seg = x[m] if m.sum() > 2 else x
        var[k] = max(float(np.var(seg)), VAR_FLOOR)
    # spread initial variances so states start distinct
    base = float(np.var(x)) or 1.0
    var = np.maximum(var, base * np.linspace(0.3, 2.5, K))
    A = np.full((K, K), 0.05 / max(K - 1, 1))
    np.fill_diagonal(A, 0.95)
    return HMMParams(np.full(K, 1.0 / K), A, mu, np.sort(var))


def fit_hmm(x, n_states: int = 2, n_iter: int = 40, tol: float = 1e-5,
            sticky_kappa: float = 50.0, dirichlet_alpha: float = 1.0,
            init: HMMParams | None = None) -> tuple[HMMParams, dict]:
    """Baum-Welch EM (MAP for the transition matrix under a sticky
    Dirichlet prior). Returns (params, info) with states ordered by
    variance."""
    x = np.asarray(x, dtype=float)
    K = int(n_states)
    p = init if init is not None and len(init.pi) == K else _init_params(x, K)
    prev_ll = -np.inf
    ll = -np.inf
    it = 0
    for it in range(1, n_iter + 1):
        B = _emission(x, p.mu, p.var)
        T = len(x)
        alpha = np.empty((T, K))
        c = np.empty(T)
        a = p.pi * B[0]
        c[0] = a.sum()
        alpha[0] = a / c[0]
        for t in range(1, T):
            a = (alpha[t - 1] @ p.A) * B[t]
            c[t] = a.sum()
            alpha[t] = a / c[t]
        ll = float(np.log(c).sum())
        beta = _backward(x, p, B, c)
        gamma = alpha * beta
        gamma /= gamma.sum(axis=1, keepdims=True)
        # expected transitions ξ summed over t
        xi = np.zeros((K, K))
        for t in range(T - 1):
            m = (alpha[t][:, None] * p.A
                 * (B[t + 1] * beta[t + 1])[None, :]) / c[t + 1]
            xi += m
        # M-step (MAP transition with sticky Dirichlet prior)
        conc = np.full((K, K), max(dirichlet_alpha, 1.0))
        conc[np.diag_indices(K)] += sticky_kappa
        num = xi + conc - 1.0 + 1e-12          # Dirichlet MAP
        A_new = num / num.sum(axis=1, keepdims=True)
        w = gamma.sum(axis=0) + 1e-12
        mu_new = (gamma * x[:, None]).sum(axis=0) / w
        var_new = (gamma * (x[:, None] - mu_new[None, :]) ** 2).sum(axis=0) / w
        var_new = np.maximum(var_new, VAR_FLOOR)
        p = HMMParams(gamma[0] * 0.5 + 0.5 / K, A_new, mu_new, var_new)
        if abs(ll - prev_ll) < tol * max(1.0, abs(ll)):
            break
        prev_ll = ll
    # order states by variance (calm → turbulent)
    order = np.argsort(p.var)
    p = HMMParams(p.pi[order], p.A[np.ix_(order, order)], p.mu[order],
                  p.var[order])
    return p, {"loglik": ll, "iters": it, "n": int(len(x))}


def regime_hmm_probabilities(bars: list, n_states: int = 2,
                             min_bars: int = HMM_MIN_BARS,
                             max_bars: int = HMM_MAX_BARS,
                             init: dict | None = None,
                             n_iter: int = 40) -> dict | None:
    """Fit on the last `max_bars` bars and return the FILTERED state
    distribution at the latest bar, or None (caller falls back)."""
    if not bars or len(bars) < min_bars:
        return None
    try:
        x = atr_normalised_returns(bars[-max_bars:])
        if len(x) < min_bars - 20:
            return None
        x = np.clip(x, -12, 12)                 # tame data glitches
        p, info = fit_hmm(x, n_states=n_states, n_iter=n_iter,
                          init=HMMParams.from_dict(init) if init else None)
        filt, _ = forward_filter(x, p)
        last = filt[-1]
        if not np.all(np.isfinite(last)):
            return None
        labels = STATE_LABELS.get(n_states,
                                  tuple(f"s{k}" for k in range(n_states)))
        sd = np.sqrt(p.var)
        return {
            "n_states": n_states,
            "probs": {labels[k]: round(float(last[k]), 4)
                      for k in range(n_states)},
            "state": labels[int(np.argmax(last))],
            "vol_ratio": round(float(sd[-1] / max(sd[0], 1e-9)), 2),
            "means": [round(float(m), 4) for m in p.mu],
            "sds": [round(float(s), 4) for s in sd],
            "persistence": [round(float(p.A[k, k]), 3)
                            for k in range(n_states)],
            "loglik": round(info["loglik"], 2), "iters": info["iters"],
            "n_obs": info["n"], "params": p.to_dict(),
        }
    except (FloatingPointError, ValueError, np.linalg.LinAlgError):
        return None
