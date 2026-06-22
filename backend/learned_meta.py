"""Learned Meta-Classifier — local logistic regression P(win | features).

Trains on the closed-trades dataset joined against the signal snapshot
that triggered each trade. Lightweight gradient-descent implementation —
no sklearn dependency.

Features used per trade (8-dim + bias):
  0. confidence_norm         signal.confidence / 100
  1. is_buy                  1 if action=BUY else 0
  2. kalman_vel_norm         kalman.k_velocity / current_price  (clamped ±0.01)
  3. cot_against             1 if BUY & overcrowded_long OR SELL & overcrowded_short
  4. cot_with                1 if BUY & overcrowded_short OR SELL & overcrowded_long
  5. tips_aligned            1 if BUY & bullish_gold OR SELL & bearish_gold
                             −1 if opposed; 0 if neutral / missing
  6. mtf_aligned             1 if mtf_gate.aligned (or no gate), else 0
  7. macro_event_24h         1 if upcoming_macro within 24h is non-empty

Label: win = 1 if trade.pnl > 0 else 0.

The veto is gated behind a minimum sample count (default 30) so we don't
fire it on noise. Threshold is calibrated on training set to deliver ~60%
precision on rejected trades (i.e. the model is right about 60% of its
"this would lose" calls).
"""
import math
import logging
from datetime import datetime, timezone
from typing import Optional
from bson import ObjectId
import numpy as np

from database import get_db

logger = logging.getLogger("learned_meta")

MIN_SAMPLES = 30
N_FEATURES = 8     # excluding bias
L2 = 0.5
LR = 0.05
EPOCHS = 400
ARTIFACT_KEY = "learned_meta_v1"


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _features_from_signal(signal: dict, current_price_fallback: float = 0.0) -> Optional[list]:
    """Extract the 8-dim feature row from a signal payload, or None on bad input."""
    if not signal:
        return None
    conf = signal.get("confidence")
    action = signal.get("action") or signal.get("chart_action")
    if conf is None or action not in ("BUY", "SELL"):
        return None

    is_buy = 1.0 if action == "BUY" else 0.0
    confidence_norm = float(conf) / 100.0

    # Kalman
    k = signal.get("kalman_filter") or {}
    k_vel = float(k.get("k_velocity") or 0.0)
    price = float(signal.get("entry_price") or current_price_fallback or 0.0)
    k_vel_norm = (k_vel / price) if price > 0 else 0.0
    k_vel_norm = max(-0.01, min(0.01, k_vel_norm))  # clamp outliers

    # COT
    cot = signal.get("cot_positioning") or {}
    over_long = bool(cot.get("overcrowded_long"))
    over_short = bool(cot.get("overcrowded_short"))
    cot_against = 1.0 if (is_buy and over_long) or (not is_buy and over_short) else 0.0
    cot_with = 1.0 if (is_buy and over_short) or (not is_buy and over_long) else 0.0

    # TIPS
    tips = signal.get("real_yield_10y") or {}
    regime = tips.get("regime") or "neutral"
    if regime == "bullish_gold":
        tips_aligned = 1.0 if is_buy else -1.0
    elif regime == "bearish_gold":
        tips_aligned = -1.0 if is_buy else 1.0
    else:
        tips_aligned = 0.0

    # MTF
    mtf = signal.get("mtf_gate") or {}
    mtf_aligned = 1.0 if mtf.get("aligned", True) else 0.0

    # Macro proximity
    upc = signal.get("upcoming_macro") or []
    macro_event_24h = 1.0 if upc else 0.0

    return [
        confidence_norm, is_buy, k_vel_norm,
        cot_against, cot_with, tips_aligned,
        mtf_aligned, macro_event_24h,
    ]


async def _build_dataset() -> tuple[np.ndarray, np.ndarray]:
    """Pull closed trades + their signals; return (X, y) numpy arrays."""
    db = get_db()
    trades = await db.trades.find(
        {"status": "closed", "signal_id": {"$ne": None}}
    ).to_list(length=5000)

    sig_ids = []
    for t in trades:
        try:
            sig_ids.append(ObjectId(t["signal_id"]))
        except Exception:
            continue
    if not sig_ids:
        return np.array([]), np.array([])
    sig_map = {}
    async for s in db.signals.find({"_id": {"$in": sig_ids}}):
        sig_map[str(s["_id"])] = s

    X, y = [], []
    for t in trades:
        sig = sig_map.get(str(t.get("signal_id") or ""))
        if not sig:
            continue
        feats = _features_from_signal(sig, current_price_fallback=float(t.get("entry_price") or 0))
        if feats is None:
            continue
        X.append(feats)
        y.append(1 if float(t.get("pnl") or 0) > 0 else 0)
    return np.array(X, dtype=float), np.array(y, dtype=float)


def _train_logreg(X: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, float]:
    """Plain L2-regularised logistic regression with GD. Returns (weights_with_bias, train_auc)."""
    n, d = X.shape
    # Standardise — mean/std per column, store for inference
    mu = X.mean(axis=0)
    sd = X.std(axis=0) + 1e-9
    Xn = (X - mu) / sd
    Xb = np.hstack([Xn, np.ones((n, 1))])   # add bias column

    w = np.zeros(d + 1)
    for _ in range(EPOCHS):
        z = Xb @ w
        p = _sigmoid(z)
        grad = (Xb.T @ (p - y)) / n + L2 * np.concatenate([w[:-1], [0]])
        w -= LR * grad

    # Simple AUC: ranks of positive class vs negative class
    p_final = _sigmoid(Xb @ w)
    auc = _auc(y, p_final) if len(set(y.tolist())) == 2 else 0.5
    return w, mu, sd, auc


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    pos = p[y == 1]
    neg = p[y == 0]
    if pos.size == 0 or neg.size == 0:
        return 0.5
    score = 0
    for pp in pos:
        score += (neg < pp).sum() + 0.5 * (neg == pp).sum()
    return float(score / (pos.size * neg.size))


async def retrain() -> dict:
    """Pull closed-trade dataset, train model, persist artifact. Returns summary."""
    db = get_db()
    X, y = await _build_dataset()
    n = len(y)
    if n < MIN_SAMPLES:
        return {
            "trained": False,
            "reason": f"need ≥{MIN_SAMPLES} closed trades (have {n})",
            "n_samples": n,
        }

    w, mu, sd, auc = _train_logreg(X, y)

    # Calibrate threshold: pick the p_win below which precision ≥ 0.6 on training set.
    p_final = _sigmoid(np.hstack([(X - mu) / sd, np.ones((n, 1))]) @ w)
    threshold = 0.45  # default if no calibration succeeds
    for cand in np.arange(0.30, 0.50, 0.01):
        rejected = p_final < cand
        if rejected.sum() == 0:
            continue
        precision_of_rejection = (y[rejected] == 0).mean()
        if precision_of_rejection >= 0.6:
            threshold = float(cand)
            break

    doc = {
        "key": ARTIFACT_KEY,
        "weights": w.tolist(),
        "mu": mu.tolist(),
        "sd": sd.tolist(),
        "threshold": threshold,
        "train_auc": round(auc, 4),
        "n_samples": int(n),
        "n_wins": int(y.sum()),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "feature_names": [
            "confidence_norm", "is_buy", "kalman_vel_norm",
            "cot_against", "cot_with", "tips_aligned",
            "mtf_aligned", "macro_event_24h",
        ],
    }
    await db.learned_meta_artifacts.update_one(
        {"key": ARTIFACT_KEY}, {"$set": doc}, upsert=True
    )
    return {"trained": True, **{k: doc[k] for k in (
        "n_samples", "n_wins", "train_auc", "threshold", "trained_at"
    )}}


async def get_artifact() -> Optional[dict]:
    db = get_db()
    doc = await db.learned_meta_artifacts.find_one({"key": ARTIFACT_KEY})
    if doc:
        doc.pop("_id", None)
    return doc


async def predict_p_win(signal: dict) -> Optional[dict]:
    """Return {p_win, threshold, verdict} or None if no trained model."""
    art = await get_artifact()
    if not art:
        return None
    feats = _features_from_signal(signal)
    if feats is None:
        return None
    try:
        w = np.array(art["weights"])
        mu = np.array(art["mu"])
        sd = np.array(art["sd"])
        threshold = float(art["threshold"])
        x = (np.array(feats) - mu) / sd
        xb = np.append(x, 1.0)
        p = float(_sigmoid(xb @ w))
        return {
            "p_win": round(p, 4),
            "threshold": round(threshold, 4),
            "verdict": "REJECT" if p < threshold else "ACCEPT",
            "n_samples": int(art.get("n_samples", 0)),
            "train_auc": float(art.get("train_auc", 0.5)),
        }
    except Exception as e:
        logger.warning("learned_meta inference failed: %s", e)
        return None
