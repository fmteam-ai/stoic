"""Scalp subsystem · Step 6 — logistic probability baseline.

Trains on barrier-labeled scalp decisions (target_first=1, stop_first=0;
timeouts excluded from the directional model per Step 7). Walk-forward OOS
AUC decides whether the model is USED — a model that can't beat 0.53 OOS
stays shelved and the deterministic forecast keeps control.
"""
import logging
import math
import time
from datetime import datetime, timezone

import numpy as np

from scalp.features import FEATURE_KEYS

logger = logging.getLogger("scalp.model")

MIN_SAMPLES = 200
MIN_OOS_AUC = 0.53
EPOCHS = 400
LR = 0.1
L2 = 1e-3
_active: dict = {}         # symbol -> {"weights","mu","sd","oos_auc","ts"}


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def _auc(y, p):
    pos, neg = p[y == 1], p[y == 0]
    if pos.size == 0 or neg.size == 0:
        return 0.5
    s = sum((neg < pp).sum() + 0.5 * (neg == pp).sum() for pp in pos)
    return float(s / (pos.size * neg.size))


def _fit(X, y):
    n, d = X.shape
    mu, sd = X.mean(axis=0), X.std(axis=0) + 1e-9
    Xb = np.hstack([(X - mu) / sd, np.ones((n, 1))])
    w = np.zeros(d + 1)
    for _ in range(EPOCHS):
        p = _sigmoid(Xb @ w)
        w -= LR * ((Xb.T @ (p - y)) / n + L2 * np.concatenate([w[:-1], [0]]))
    return w, mu, sd


def _predict(w, mu, sd, X):
    Xb = np.hstack([(X - mu) / sd, np.ones((len(X), 1))])
    return _sigmoid(Xb @ w)


def vectorize(features: dict) -> list | None:
    try:
        return [float(features[k]) for k in FEATURE_KEYS]
    except (KeyError, TypeError, ValueError):
        return None


async def retrain(db, symbol: str) -> dict:
    """Chronological walk-forward: train on past, score AUC strictly OOS."""
    cur = db.scalp_decisions.find(
        {"symbol": symbol, "outcome.result": {"$in": ["target_first", "stop_first"]}},
        {"features": 1, "outcome": 1, "ts_ms": 1}).sort("ts_ms", 1)
    docs = await cur.to_list(20_000)
    X, y = [], []
    for d in docs:
        v = vectorize(d.get("features") or {})
        if v is None:
            continue
        X.append(v)
        y.append(1.0 if d["outcome"]["result"] == "target_first" else 0.0)
    n = len(y)
    if n < MIN_SAMPLES or len(set(y)) < 2:
        return {"trained": False, "reason": f"insufficient labeled samples ({n}/{MIN_SAMPLES})",
                "n": n}
    X, y = np.array(X), np.array(y)

    # expanding-window OOS predictions over the last 60%
    start = max(50, int(n * 0.4))
    bounds = np.linspace(start, n, 5).astype(int)
    oos_p, oos_y = [], []
    for k in range(4):
        lo, hi = int(bounds[k]), int(bounds[k + 1])
        if hi <= lo or len(set(y[:lo].tolist())) < 2:
            continue
        w, mu, sd = _fit(X[:lo], y[:lo])
        oos_p.extend(_predict(w, mu, sd, X[lo:hi]).tolist())
        oos_y.extend(y[lo:hi].tolist())
    if len(oos_p) < 30:
        return {"trained": False, "reason": "too few OOS predictions", "n": n}
    oos_auc = round(_auc(np.array(oos_y), np.array(oos_p)), 4)

    # deploy model trained on ALL data; quality metric is the OOS AUC
    w, mu, sd = _fit(X, y)
    artifact = {
        "symbol": symbol, "n_samples": int(n),
        "weights": w.tolist(), "mu": mu.tolist(), "sd": sd.tolist(),
        "oos_auc": oos_auc, "feature_keys": FEATURE_KEYS,
        "usable": oos_auc >= MIN_OOS_AUC,
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.scalp_models.update_one({"symbol": symbol}, {"$set": artifact},
                                     upsert=True)
    if artifact["usable"]:
        _active[symbol] = {"w": w, "mu": mu, "sd": sd,
                           "oos_auc": oos_auc, "ts": time.time()}
    else:
        _active.pop(symbol, None)
    logger.info("scalp model %s retrained n=%d oos_auc=%.3f usable=%s",
                symbol, n, oos_auc, artifact["usable"])
    return {"trained": True, "n": n, "oos_auc": oos_auc,
            "usable": artifact["usable"]}


def predict_p(symbol: str, features: dict) -> float | None:
    """None → caller keeps the deterministic baseline."""
    ent = _active.get(symbol)
    if ent is None:
        return None
    v = vectorize(features)
    if v is None:
        return None
    p = _predict(ent["w"], ent["mu"], ent["sd"], np.array([v]))
    return float(p[0])


async def load_persisted(db, symbol: str) -> bool:
    doc = await db.scalp_models.find_one({"symbol": symbol, "usable": True})
    if not doc:
        return False
    _active[symbol] = {"w": np.array(doc["weights"]), "mu": np.array(doc["mu"]),
                       "sd": np.array(doc["sd"]), "oos_auc": doc["oos_auc"],
                       "ts": time.time()}
    return True
