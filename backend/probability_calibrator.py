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

At inference, calibrated p_win = sigmoid(A*z + B).

Notes
-----
* The "Platt prior" smoothing (Lin et al. 2007) replaces 0/1 labels with
  (1/(N⁻+2), (N⁺+1)/(N⁺+2)) to avoid the perfect-separation problem
  on tiny datasets. Implemented below.
* When N < 10 we skip calibration entirely (A=1, B=0 → identity).
* Pure NumPy — no sklearn dependency to keep the stack lean.
* C1 (roadmap step 1): the previous sign convention sigmoid(-(A*z+B)) made
  the A=1,B=0 "identity" an INVERSION (0.8 → 0.2) and started gradient descent
  on the wrong side, so under-converged fits anti-ranked setups. Artifacts
  now carry `sign="standard"`; legacy artifacts without it are ignored
  (raw p is used) until the model worker refits.
"""
from __future__ import annotations
import logging
import numpy as np

logger = logging.getLogger("probability_calibrator")

_EPS = 1e-12
PLATT_SIGN = "standard"  # p_cal = sigmoid(A*z + B); artifacts without this marker are legacy
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
        return {"A": 1.0, "B": 0.0, "n": int(n), "sign": PLATT_SIGN,
                "converged": False, "skipped": True,
                "reason": f"need ≥10 samples for calibration (have {n})"}

    # Platt's smoothed targets to prevent overfitting on small data.
    n_pos = float(np.sum(labels == 1))
    n_neg = float(np.sum(labels == 0))
    t_pos = (n_pos + 1.0) / (n_pos + 2.0) if n_pos > 0 else 0.5
    t_neg = 1.0 / (n_neg + 2.0) if n_neg > 0 else 0.5
    targets = np.where(labels == 1, t_pos, t_neg)

    z = _logit(scores)
    A = 1.0
    B = 0.0
    prev_loss = float("inf")
    for it in range(max_iter):
        # p = sigmoid(A*z + B): A=1,B=0 is the identity, so GD starts from
        # "trust the raw score" and only bends it where the labels disagree.
        lin = A * z + B
        p = _sigmoid(lin)
        # NLL = -[t*log(p) + (1-t)*log(1-p)]
        loss = -np.mean(targets * np.log(p + _EPS) +
                        (1 - targets) * np.log(1 - p + _EPS))
        grad_A = np.mean((p - targets) * z)
        grad_B = np.mean(p - targets)
        A -= lr * grad_A
        B -= lr * grad_B
        if abs(prev_loss - loss) < 1e-7:
            return {"A": float(A), "B": float(B), "n": int(n), "sign": PLATT_SIGN,
                    "converged": True, "skipped": False, "iters": it + 1,
                    "final_nll": float(loss)}
        prev_loss = loss
    return {"A": float(A), "B": float(B), "n": int(n), "sign": PLATT_SIGN,
            "converged": False, "skipped": False, "iters": max_iter,
            "final_nll": float(loss)}


def platt_is_valid(calib: dict | None) -> bool:
    """True only for a fitted artifact produced under the standard sign convention."""
    return bool(calib) and not calib.get("skipped") and calib.get("sign") == PLATT_SIGN


def apply_platt(raw_p: float, calib: dict | None) -> float:
    """Map a raw probability through the persisted (A, B). No-op if missing,
    skipped, or a legacy artifact fitted under the inverted convention."""
    if not platt_is_valid(calib):
        return float(raw_p)
    A = float(calib.get("A", 1.0))
    B = float(calib.get("B", 0.0))
    z = _logit(np.asarray(raw_p, dtype=float))
    p_cal = _sigmoid(A * z + B)
    return float(p_cal)


def brier_score(scores: np.ndarray, labels: np.ndarray) -> float:
    """Mean-squared error between predicted probability and outcome.

    Lower = better-calibrated. Used to confirm post-calibration improves.
    """
    if len(labels) == 0:
        return 0.0
    return float(np.mean((np.asarray(scores) - np.asarray(labels)) ** 2))
