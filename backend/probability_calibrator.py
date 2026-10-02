"""Probabilistic calibration for the learned-meta classifier (iter-52).

Raw logistic-regression sigmoid outputs are NOT calibrated probabilities —
they're class-membership scores. A model predicting `p=0.7` does not
necessarily mean wins occur 70% of the time at that score.

This module implements **Platt scaling** (Platt 1999): fit a 2-parameter
logistic on the (logit, label) pairs to map raw scores → calibrated
probabilities.

Algorithm
=========
1. After training the base model, compute predicted scores p_i on a
   TRUE held-out set: walk-forward out-of-sample folds when available,
   else a chronological tail holdout once n ≥ HOLDOUT_MIN_N (iter-191 —
   resolves the long-standing TODO), else in-sample as a last resort for
   tiny datasets (flagged as such in the artifact).
2. Compute logits z_i = logit(p_i).
3. Fit `p_cal = 1 / (1 + exp(A*z + B))` by minimising NLL via GD.
4. Persist (A, B) alongside the artifact, plus Brier + ECE so calibration
   error is TRACKED over time (db.calibration_history).

At inference, calibrated p_win = sigmoid(-(A*z + B)).

Notes
-----
* The "Platt prior" smoothing (Lin et al. 2007) replaces 0/1 labels with
  (1/(N⁻+2), (N⁺+1)/(N⁺+2)) to avoid the perfect-separation problem
  on tiny datasets. Implemented below.
* When N < 10 we skip calibration entirely (A=1, B=0 → identity).
* Pure NumPy — no sklearn dependency to keep the stack lean.
"""
from __future__ import annotations
import logging
import numpy as np

logger = logging.getLogger("probability_calibrator")

_EPS = 1e-12
HOLDOUT_MIN_N = 100      # chronological-tail holdout kicks in at this size
HOLDOUT_FRACTION = 0.3   # last 30% of samples reserved for calibration


def holdout_tail_indices(n: int) -> np.ndarray | None:
    """Chronological tail indices for true held-out calibration, or None
    when the dataset is too small (< HOLDOUT_MIN_N)."""
    if n < HOLDOUT_MIN_N:
        return None
    start = int(n * (1.0 - HOLDOUT_FRACTION))
    return np.arange(start, n)


def expected_calibration_error(scores: np.ndarray, labels: np.ndarray,
                               bins: int = 10) -> float:
    """ECE — mean |predicted − observed| across probability bins, weighted
    by bin population. 0 = perfectly calibrated."""
    scores = np.asarray(scores, dtype=float)
    labels = np.asarray(labels, dtype=float)
    if len(labels) == 0:
        return 0.0
    edges = np.linspace(0.0, 1.0, bins + 1)
    ece = 0.0
    for i in range(bins):
        mask = (scores >= edges[i]) & (scores < edges[i + 1] if i < bins - 1
                                       else scores <= edges[i + 1])
        if not mask.any():
            continue
        ece += (mask.mean()
                * abs(float(scores[mask].mean())
                      - float(labels[mask].mean())))
    return round(float(ece), 4)


def _logit(p: float | np.ndarray) -> float | np.ndarray:
    p = np.clip(p, _EPS, 1.0 - _EPS)
    return np.log(p / (1.0 - p))


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def fit_platt(scores: np.ndarray, labels: np.ndarray,
              max_iter: int = 200, lr: float = 0.05) -> dict:
    """Fit Platt scaling. Returns {A, B, n, converged, label_smooth}.

    `scores` are model probability outputs in (0,1). `labels` ∈ {0,1}.
    """
    n = len(labels)
    if n < 10:
        return {"A": 1.0, "B": 0.0, "n": int(n),
                "converged": False, "skipped": True,
                "reason": f"need ≥10 samples for calibration (have {n})"}

    # Platt's smoothed targets to prevent overfitting on small data.
    n_pos = float(np.sum(labels == 1))
    n_neg = float(np.sum(labels == 0))
    t_pos = (n_pos + 1.0) / (n_pos + 2.0) if n_pos > 0 else 0.5
    t_neg = 1.0 / (n_neg + 2.0) if n_neg > 0 else 0.5
    targets = np.where(labels == 1, t_pos, t_neg)

    z = _logit(scores)
    # p = sigmoid(-(A*z + B)) — Platt's original sign convention, so the
    # IDENTITY mapping is A = -1, B = 0. Starting at A = +1 (the old
    # initialisation) is the map p -> 1-p, which first-order descent could
    # not escape within its iteration budget and produced calibrated
    # probabilities anti-correlated with the raw model.
    #
    # Two-parameter convex NLL -> damped Newton-Raphson converges in a
    # handful of iterations; no learning rate to tune. `lr` is kept in the
    # signature for backwards compatibility and unused.
    del lr
    A = -1.0
    B = 0.0

    def _nll(a: float, b: float) -> float:
        pp = _sigmoid(-(a * z + b))
        return float(-np.mean(targets * np.log(pp + _EPS) +
                              (1 - targets) * np.log(1 - pp + _EPS)))

    loss = _nll(A, B)
    for it in range(max_iter):
        p = _sigmoid(-(A * z + B))
        # d loss / d lin = (t - p) with lin = A*z + B
        r = targets - p
        g = np.array([np.mean(r * z), np.mean(r)])
        w = p * (1.0 - p)
        H = np.array([[np.mean(w * z * z), np.mean(w * z)],
                      [np.mean(w * z), np.mean(w)]]) + 1e-9 * np.eye(2)
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        # Backtracking line search keeps every step a descent step.
        t = 1.0
        while t > 1e-6:
            a_new, b_new = A - t * step[0], B - t * step[1]
            new_loss = _nll(a_new, b_new)
            if new_loss <= loss:
                break
            t *= 0.5
        else:
            return {"A": float(A), "B": float(B), "n": int(n),
                    "converged": True, "skipped": False, "iters": it + 1,
                    "final_nll": float(loss)}
        improved = loss - new_loss
        A, B, loss = float(a_new), float(b_new), new_loss
        if improved < 1e-10:
            return {"A": float(A), "B": float(B), "n": int(n),
                    "converged": True, "skipped": False, "iters": it + 1,
                    "final_nll": float(loss)}
    return {"A": float(A), "B": float(B), "n": int(n),
            "converged": False, "skipped": False, "iters": max_iter,
            "final_nll": float(loss)}


def apply_platt(raw_p: float, calib: dict | None) -> float:
    """Map a raw probability through the persisted (A, B). No-op if missing."""
    if not calib or calib.get("skipped"):
        return float(raw_p)
    A = float(calib.get("A", -1.0))   # -1 == identity (see fit_platt)
    B = float(calib.get("B", 0.0))
    z = _logit(np.asarray(raw_p, dtype=float))
    p_cal = _sigmoid(-(A * z + B))
    return float(p_cal)


def brier_score(scores: np.ndarray, labels: np.ndarray) -> float:
    """Mean-squared error between predicted probability and outcome.

    Lower = better-calibrated. Used to confirm post-calibration improves.
    """
    if len(labels) == 0:
        return 0.0
    return float(np.mean((np.asarray(scores) - np.asarray(labels)) ** 2))
