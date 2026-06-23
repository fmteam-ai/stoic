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

MIN_SAMPLES = 30                    # global minimum to train at all
MIN_SAMPLES_PER_SESSION = 25        # per-session minimum to train a session-specific model
N_FEATURES = 8     # excluding bias
L2 = 0.5
LR = 0.05
EPOCHS = 400
ARTIFACT_KEY = "learned_meta_v1"
SESSION_ARTIFACT_PREFIX = "learned_meta_v1_session_"   # + ASIA / LONDON / NY


def session_bucket(dt) -> str:
    """Bucket a UTC datetime into one of: ASIA / LONDON / NY / OFF.

    London-NY overlap (13:00-16:00 UTC) is bucketed under NY since most
    institutional flow during the overlap leans on US data.
    Anything else (22:00-00:00 UTC) is OFF — these trades are dropped from
    session-specific training and fall back to the global model at inference.
    """
    if dt is None:
        return "OFF"
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return "OFF"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    h = dt.hour
    if 0 <= h < 7:
        return "ASIA"
    if 7 <= h < 13:
        return "LONDON"
    if 13 <= h < 22:
        return "NY"
    return "OFF"


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


async def _build_dataset() -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Pull closed trades + their signals; return (X, y, sessions)."""
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
        return np.array([]), np.array([]), []
    sig_map = {}
    async for s in db.signals.find({"_id": {"$in": sig_ids}}):
        sig_map[str(s["_id"])] = s

    X, y, sessions = [], [], []
    for t in trades:
        sig = sig_map.get(str(t.get("signal_id") or ""))
        if not sig:
            continue
        feats = _features_from_signal(sig, current_price_fallback=float(t.get("entry_price") or 0))
        if feats is None:
            continue
        # Bucket by ENTRY time, not close time — the model conditions on
        # context at trade-open, which is what inference sees.
        entered_at = t.get("entered_at") or t.get("opened_at") or sig.get("created_at")
        sessions.append(session_bucket(entered_at))
        X.append(feats)
        y.append(1 if float(t.get("pnl") or 0) > 0 else 0)
    return np.array(X, dtype=float), np.array(y, dtype=float), sessions


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


def _fit_artifact(X: np.ndarray, y: np.ndarray, key: str, label: str) -> dict:
    """Train one logistic model on (X, y) and return the persistable artifact dict.

    Caller is responsible for sample-count gating + persistence.
    """
    n = len(y)
    w, mu, sd, auc = _train_logreg(X, y)
    # Calibrate threshold: lowest p_win below which precision_of_rejection >= 0.6
    p_final = _sigmoid(np.hstack([(X - mu) / sd, np.ones((n, 1))]) @ w)
    threshold = 0.45
    for cand in np.arange(0.30, 0.50, 0.01):
        rejected = p_final < cand
        if rejected.sum() == 0:
            continue
        if (y[rejected] == 0).mean() >= 0.6:
            threshold = float(cand)
            break
    return {
        "key": key,
        "label": label,
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


async def retrain() -> dict:
    """Pull closed-trade dataset, train global + per-session models. Persist artifacts."""
    db = get_db()
    X, y, sessions = await _build_dataset()
    n = len(y)
    if n < MIN_SAMPLES:
        return {
            "trained": False,
            "reason": f"need ≥{MIN_SAMPLES} closed trades (have {n})",
            "n_samples": n,
        }

    # 1. Global fallback artifact — always trained when there's enough data
    global_doc = _fit_artifact(X, y, ARTIFACT_KEY, "GLOBAL")
    await db.learned_meta_artifacts.update_one(
        {"key": ARTIFACT_KEY}, {"$set": global_doc}, upsert=True
    )

    # 2. Per-session artifacts — only trained when each bucket has enough data
    sessions_arr = np.array(sessions)
    per_session = {}
    for label in ("ASIA", "LONDON", "NY"):
        mask = sessions_arr == label
        n_s = int(mask.sum())
        wins_s = int(y[mask].sum()) if n_s else 0
        # Need both classes (≥1 win and ≥1 loss) — pure 0/1 sets degenerate LR
        if n_s >= MIN_SAMPLES_PER_SESSION and 0 < wins_s < n_s:
            key = f"{SESSION_ARTIFACT_PREFIX}{label}"
            doc = _fit_artifact(X[mask], y[mask], key, label)
            await db.learned_meta_artifacts.update_one(
                {"key": key}, {"$set": doc}, upsert=True
            )
            per_session[label] = {
                "trained": True, "n_samples": n_s, "n_wins": wins_s,
                "train_auc": doc["train_auc"], "threshold": doc["threshold"],
            }
        else:
            # Wipe stale per-session artifact so we don't predict on outdated weights
            await db.learned_meta_artifacts.delete_one(
                {"key": f"{SESSION_ARTIFACT_PREFIX}{label}"}
            )
            per_session[label] = {
                "trained": False, "n_samples": n_s, "n_wins": wins_s,
                "reason": f"need ≥{MIN_SAMPLES_PER_SESSION} samples + both classes",
            }

    return {
        "trained": True,
        "global": {k: global_doc[k] for k in ("n_samples", "n_wins", "train_auc", "threshold", "trained_at")},
        "per_session": per_session,
    }


async def get_artifact(session_label: Optional[str] = None) -> Optional[dict]:
    """Return the per-session artifact for `session_label`, else the global one."""
    db = get_db()
    if session_label and session_label in ("ASIA", "LONDON", "NY"):
        doc = await db.learned_meta_artifacts.find_one(
            {"key": f"{SESSION_ARTIFACT_PREFIX}{session_label}"}
        )
        if doc:
            doc.pop("_id", None)
            return doc
    doc = await db.learned_meta_artifacts.find_one({"key": ARTIFACT_KEY})
    if doc:
        doc.pop("_id", None)
    return doc


async def predict_p_win(signal: dict) -> Optional[dict]:
    """Return {p_win, threshold, verdict, model_used} or None if no model.

    Picks the session-specific model for the CURRENT session if available;
    otherwise falls back to the global model.
    """
    current_session_label = session_bucket(datetime.now(timezone.utc))
    art = await get_artifact(session_label=current_session_label)
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
            "model_used": art.get("label", "GLOBAL"),
            "current_session": current_session_label,
        }
    except Exception as e:
        logger.warning("learned_meta inference failed: %s", e)
        return None
