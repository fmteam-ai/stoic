"""Scalp subsystem · logistic probability model with CALIBRATION and
profitability-based deployment gates (review items 2/3/4/12).

- Models are keyed by broker | account_type | symbol — never pooled across
  execution environments.
- Chronological walk-forward produces OOS predictions; PLATT scaling is fit
  on the FIRST HALF of the OOS block, all quality metrics (AUC, Brier vs
  baseline, ECE, net expectancy, stress, rolling windows) are computed on
  the SECOND half — never on anything the calibrator saw.
- `usable` requires ALL of: AUC ≥ 0.55, calibrated Brier ≤ baseline Brier,
  positive OOS net expectancy on model-selected trades, positive under +50%
  cost stress, ≥65% profitable rolling windows, n ≥ 300.
- Artifacts EXPIRE after 7 days; predict_p also refuses out-of-distribution
  feature vectors (|z| > 6) — both fall back to the deterministic baseline.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from scalp.features import FEATURE_KEYS

logger = logging.getLogger("scalp.model")

MIN_SAMPLES = 300
MIN_OOS_AUC = 0.55
MIN_PROFITABLE_WINDOWS = 0.65
COST_STRESS_MULT = 1.5
MODEL_TTL_DAYS = 7
OOD_Z_LIMIT = 6.0
EPOCHS = 400
LR = 0.1
L2 = 1e-3
_active: dict = {}          # model_key -> artifact dict


def make_key(broker: str, account_type: str, symbol: str) -> str:
    return f"{(broker or 'any').strip()}|{(account_type or 'any').strip()}|{symbol.upper()}"


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


def fit_platt(p_raw, y):
    """1-D logistic on logit(p): returns (a, b) with p_cal = σ(a·logit(p)+b)."""
    z = np.log(np.clip(p_raw, 1e-6, 1 - 1e-6) / (1 - np.clip(p_raw, 1e-6, 1 - 1e-6)))
    a, b = 1.0, 0.0
    for _ in range(300):
        p = _sigmoid(a * z + b)
        ga = np.mean((p - y) * z)
        gb = np.mean(p - y)
        a -= 0.5 * ga
        b -= 0.5 * gb
    return float(a), float(b)


def apply_platt(p_raw, a, b):
    p_raw = np.clip(p_raw, 1e-6, 1 - 1e-6)
    z = np.log(p_raw / (1 - p_raw))
    return _sigmoid(a * z + b)


def brier(p, y):
    return float(np.mean((p - y) ** 2))


def ece(p, y, bins=10):
    """Expected calibration error."""
    total, err = len(p), 0.0
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        m = (p >= lo) & (p < hi) if i < bins - 1 else (p >= lo) & (p <= hi)
        if m.sum() == 0:
            continue
        err += (m.sum() / total) * abs(p[m].mean() - y[m].mean())
    return float(err)


def vectorize(features: dict) -> list | None:
    try:
        return [float(features[k]) for k in FEATURE_KEYS]
    except (KeyError, TypeError, ValueError):
        return None


def _trade_selected(p_cal, target, stop, cost):
    return p_cal * target - (1 - p_cal) * stop - cost > 0


async def retrain(db, symbol: str, broker: str = "any",
                  account_type: str = "any") -> dict:
    key = make_key(broker, account_type, symbol)
    q = {"symbol": symbol.upper(),
         "outcome.result": {"$in": ["target_first", "stop_first"]}}
    if broker and broker != "any":
        q["broker"] = broker
    if account_type and account_type != "any":
        q["account_type"] = account_type
    cur = db.scalp_decisions.find(
        q, {"features": 1, "outcome": 1, "ts_ms": 1, "forecast": 1,
            "cost_pips": 1}).sort("ts_ms", 1)
    docs = await cur.to_list(20_000)
    X, y, net, tgt, stp, cost = [], [], [], [], [], []
    for d in docs:
        v = vectorize(d.get("features") or {})
        if v is None:
            continue
        fc = d.get("forecast") or {}
        X.append(v)
        y.append(1.0 if d["outcome"]["result"] == "target_first" else 0.0)
        net.append(float(d["outcome"].get("net_pips") or 0))
        tgt.append(float(fc.get("target_pips") or 2.0))
        stp.append(float(fc.get("stop_pips") or 2.0))
        cost.append(float(d.get("cost_pips") or 1.0))
    n = len(y)
    if n < MIN_SAMPLES or len(set(y)) < 2:
        return {"trained": False, "model_key": key, "n": n,
                "reason": f"insufficient labeled samples ({n}/{MIN_SAMPLES})"}
    X, y = np.array(X), np.array(y)
    net, tgt, stp, cost = map(np.array, (net, tgt, stp, cost))

    # ---- chronological split: train 40% | calibrate 30% | evaluate 30% ----
    i_cal = max(50, int(n * 0.4))
    i_eval = int(n * 0.7)
    if len(set(y[:i_cal].tolist())) < 2 or n - i_eval < 30:
        return {"trained": False, "model_key": key, "n": n,
                "reason": "insufficient class balance / eval window"}
    w, mu, sd = _fit(X[:i_cal], y[:i_cal])
    p_cal_win = _predict(w, mu, sd, X[i_cal:i_eval])
    a, b = fit_platt(p_cal_win, y[i_cal:i_eval])

    p_eval_raw = _predict(w, mu, sd, X[i_eval:])
    p_eval = apply_platt(p_eval_raw, a, b)
    y_eval = y[i_eval:]
    oos_auc = round(_auc(y_eval, p_eval), 4)
    b_model = round(brier(p_eval, y_eval), 4)
    b_base = round(brier(np.full_like(y_eval, y[:i_eval].mean()), y_eval), 4)
    ece_v = round(ece(p_eval, y_eval), 4)

    # ---- profitability gates on model-SELECTED trades ----
    sel = _trade_selected(p_eval, tgt[i_eval:], stp[i_eval:], cost[i_eval:])
    net_eval = net[i_eval:]
    if sel.sum() >= 10:
        oos_net_exp = round(float(net_eval[sel].mean()), 3)
        stressed = net_eval[sel] - (COST_STRESS_MULT - 1.0) * cost[i_eval:][sel]
        stressed_exp = round(float(stressed.mean()), 3)
        wins = float(net_eval[sel][net_eval[sel] > 0].sum())
        losses = abs(float(net_eval[sel][net_eval[sel] < 0].sum()))
        profit_factor = round(wins / losses, 2) if losses > 0 else None
        k = max(3, int(sel.sum() // 25))
        idx = np.where(sel)[0]
        windows = np.array_split(net_eval[idx], min(4, k))
        prof_ratio = round(float(np.mean([wd.mean() > 0 for wd in windows
                                          if len(wd)])), 2)
    else:
        oos_net_exp = stressed_exp = prof_ratio = None
        profit_factor = None

    usable = bool(
        oos_auc >= MIN_OOS_AUC
        and b_model <= b_base
        and oos_net_exp is not None and oos_net_exp > 0
        and stressed_exp is not None and stressed_exp > 0
        and prof_ratio is not None and prof_ratio >= MIN_PROFITABLE_WINDOWS
    )

    # deploy: retrain base model on train+cal, keep the held-out calibrator
    w_f, mu_f, sd_f = _fit(X[:i_eval], y[:i_eval])
    expires = (datetime.now(timezone.utc) + timedelta(days=MODEL_TTL_DAYS)).isoformat()
    artifact = {
        "model_key": key, "symbol": symbol.upper(),
        "broker": broker, "account_type": account_type,
        "n_samples": int(n),
        "weights": w_f.tolist(), "mu": mu_f.tolist(), "sd": sd_f.tolist(),
        "platt_a": a, "platt_b": b,
        "oos_auc": oos_auc, "brier": b_model, "brier_baseline": b_base,
        "ece": ece_v,
        "oos_net_expectancy_pips": oos_net_exp,
        "stressed_net_expectancy_pips": stressed_exp,
        "profit_factor": profit_factor,
        "profitable_windows_ratio": prof_ratio,
        "usable": usable,
        "feature_keys": FEATURE_KEYS,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires,
    }
    await db.scalp_models.update_one({"model_key": key}, {"$set": artifact},
                                     upsert=True)
    if usable:
        _active[key] = _to_runtime(artifact)
    else:
        _active.pop(key, None)
    logger.info("scalp model %s n=%d auc=%.3f brier=%.4f/%.4f netexp=%s usable=%s",
                key, n, oos_auc, b_model, b_base, oos_net_exp, usable)
    return {"trained": True, "model_key": key, "n": n, "oos_auc": oos_auc,
            "brier": b_model, "brier_baseline": b_base, "ece": ece_v,
            "oos_net_expectancy_pips": oos_net_exp,
            "stressed_net_expectancy_pips": stressed_exp,
            "profitable_windows_ratio": prof_ratio, "usable": usable}


def _to_runtime(artifact: dict) -> dict:
    return {"w": np.array(artifact["weights"]), "mu": np.array(artifact["mu"]),
            "sd": np.array(artifact["sd"]),
            "platt_a": artifact["platt_a"], "platt_b": artifact["platt_b"],
            "expires_at": artifact["expires_at"], "oos_auc": artifact["oos_auc"],
            "ts": time.time()}


def predict_p(model_key: str, features: dict) -> float | None:
    """Calibrated probability, or None → deterministic baseline keeps control.

    Refuses expired artifacts (item 12) and out-of-distribution inputs."""
    ent = _active.get(model_key)
    if ent is None:
        return None
    try:
        if datetime.fromisoformat(ent["expires_at"]) < datetime.now(timezone.utc):
            _active.pop(model_key, None)
            return None
    except (ValueError, TypeError):
        return None
    v = vectorize(features)
    if v is None:
        return None
    x = np.array(v)
    z = np.abs((x - ent["mu"]) / ent["sd"])
    if float(z.max()) > OOD_Z_LIMIT:
        return None                       # feature drift / OOD guard
    p_raw = _predict(ent["w"], ent["mu"], ent["sd"], x.reshape(1, -1))[0]
    return float(apply_platt(np.array([p_raw]), ent["platt_a"], ent["platt_b"])[0])


async def load_persisted(db, model_key: str) -> bool:
    doc = await db.scalp_models.find_one({"model_key": model_key, "usable": True})
    if not doc:
        return False
    try:
        if datetime.fromisoformat(doc["expires_at"]) < datetime.now(timezone.utc):
            return False
    except (KeyError, ValueError, TypeError):
        return False
    _active[model_key] = _to_runtime(doc)
    return True
