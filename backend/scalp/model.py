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
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from scalp.features import FEATURE_KEYS
from scalp.feature_schema import FEATURE_SCHEMA_VERSION, keys_for

logger = logging.getLogger("scalp.model")

MIN_SAMPLES = 300
MIN_OOS_AUC = 0.55
MIN_SELECTED_TRADES = 200      # round 4 item 9: 10 was far too small
MIN_PROFITABLE_WINDOWS = 0.65
MIN_ROLLING_POSITIVE = 2 / 3   # rolling shifted-window promotions (item 8)
COST_STRESS_MULT = 1.5
MODEL_TTL_DAYS = 7
OOD_Z_LIMIT = 6.0
EMBARGO_MS = 300_000           # = max holding period → label overlap window
BOOTSTRAP_BLOCK = 10
BOOTSTRAP_ITERS = 500
BOOTSTRAP_SEED = 42
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


def vectorize(features: dict, schema_version: int | None = None) -> list | None:
    keys = keys_for(schema_version)
    if keys is None:
        return None
    try:
        return [float(features[k]) for k in keys]
    except (KeyError, TypeError, ValueError):
        return None


def _trade_selected(p_cal, target, stop, cost):
    return p_cal * target - (1 - p_cal) * stop - cost > 0


def _lower_bound(vals: np.ndarray) -> float:
    """One-sided 95% lower bound on the mean via BLOCK bootstrap (round 5
    item 6). Nearby scalps share impulses, spread regimes and future paths —
    resampling CONTIGUOUS blocks preserves that serial dependence, so the
    bound reflects the effective independent sample size, not raw count."""
    n = len(vals)
    if n < 2:
        return float("-inf")
    block = min(BOOTSTRAP_BLOCK, max(1, n // 5))
    n_blocks = int(np.ceil(n / block))
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    starts = rng.integers(0, n - block + 1, size=(BOOTSTRAP_ITERS, n_blocks))
    offs = np.arange(block)
    means = np.empty(BOOTSTRAP_ITERS)
    for i in range(BOOTSTRAP_ITERS):
        idx = (starts[i][:, None] + offs[None, :]).ravel()[:n]
        means[i] = vals[idx].mean()
    return float(np.quantile(means, 0.05))


def _purge(mask: np.ndarray, ts: np.ndarray, boundary_idx: int) -> np.ndarray:
    """Round 5 item 7 — purge + embargo: drop train/cal samples whose label
    window (max holding period) can overlap the next fold's first sample."""
    if boundary_idx >= len(ts) or boundary_idx <= 0:
        return mask
    cutoff = ts[boundary_idx] - EMBARGO_MS
    out = mask.copy()
    idx = np.arange(len(ts))
    out[(idx < boundary_idx) & (ts >= cutoff)] = False
    return out


def _rolling_positive_ratio(X, y, res, net, tgt, stp, cost, n, ts=None) -> float | None:
    """Item 8 — three shifted train/cal/eval windows; ratio with a positive
    net expectancy on model-selected trades. Guards promotion against
    overfitting one favorable final block. Folds are PURGED + EMBARGOED."""
    ratios = []
    for f_tr, f_cal, f_ev in ((0.35, 0.50, 0.65), (0.45, 0.60, 0.75),
                              (0.55, 0.70, 0.85)):
        i1, i2, i3 = int(n * f_tr), int(n * f_cal), int(n * f_ev)
        dm = res != "timeout"
        m_tr = dm.copy(); m_tr[i1:] = False
        m_cl = dm.copy(); m_cl[:i1] = False; m_cl[i2:] = False
        if ts is not None:
            m_tr = _purge(m_tr, ts, i1)
            m_cl = _purge(m_cl, ts, i2)
        if m_tr.sum() < 40 or m_cl.sum() < 20 or i3 - i2 < 20:
            continue
        if len(set(y[m_tr].tolist())) < 2:
            continue
        w, mu, sd = _fit(X[m_tr], y[m_tr])
        a, b = fit_platt(_predict(w, mu, sd, X[m_cl]), y[m_cl])
        m_ev = np.zeros(n, dtype=bool); m_ev[i2:i3] = True
        p = apply_platt(_predict(w, mu, sd, X[m_ev]), a, b)
        sel = _trade_selected(p, tgt[m_ev], stp[m_ev], cost[m_ev])
        if sel.sum() < 10:
            continue
        ratios.append(1.0 if float(net[m_ev][sel].mean()) > 0 else 0.0)
    return round(float(np.mean(ratios)), 2) if len(ratios) >= 2 else None


# Single-flight per model key: concurrent retrain requests for the same key
# (API route, engine auto-retrain, scheduled maintenance) join the in-flight
# run instead of training twice. Keyed by (event loop, model key).
_inflight: dict = {}


async def retrain(db, symbol: str, broker: str = "any",
                  account_type: str = "any") -> dict:
    """Single-flight wrapper around _retrain_once (see its docstring). A
    caller arriving while the same model key is training awaits that run and
    receives its result with "coalesced": True."""
    key = make_key(broker, account_type, symbol)
    loop = asyncio.get_running_loop()
    slot = (id(loop), key)
    running = _inflight.get(slot)
    if running is not None and not running.done():
        out = await asyncio.shield(running)
        return {**out, "coalesced": True}
    task = loop.create_task(_retrain_once(db, symbol, broker, account_type),
                            name=f"scalp-retrain:{key}")
    _inflight[slot] = task
    task.add_done_callback(
        lambda t: _inflight.pop(slot, None) if _inflight.get(slot) is t else None)
    return await asyncio.shield(task)


def retrain_in_progress(symbol: str, broker: str = "any",
                        account_type: str = "any") -> bool:
    key = make_key(broker, account_type, symbol)
    return any(k == key and not t.done() for (_l, k), t in _inflight.items())


async def _retrain_once(db, symbol: str, broker: str = "any",
                        account_type: str = "any") -> dict:
    """Option C design (review): train 50% | calibrate 20% | evaluate 30%.
    The DEPLOYED artifact is the 50%-trained base model WITH its own Platt
    calibrator — the model is NEVER retrained after calibration, so the
    calibrator always corresponds to the deployed weights.

    Timeout outcomes are excluded from the DIRECTIONAL label (target-before-
    stop) but INCLUDED in every profitability/deployment calculation."""
    key = make_key(broker, account_type, symbol)
    q = {"symbol": symbol.upper(),
         "outcome.result": {"$in": ["target_first", "stop_first", "timeout"]}}
    if broker and broker != "any":
        q["broker"] = broker
    if account_type and account_type != "any":
        q["account_type"] = account_type
    cur = db.scalp_decisions.find(
        q, {"features": 1, "outcome": 1, "ts_ms": 1, "forecast": 1,
            "cost_pips": 1, "feature_schema_version": 1}).sort("ts_ms", 1)
    docs = await cur.to_list(20_000)
    # CPU-bound fit/calibration/bootstrap runs in a worker thread — never on
    # the event loop (a 20k-row retrain blocked the API for seconds).
    result, artifact = await asyncio.to_thread(
        _train_from_docs, docs, key, symbol, broker, account_type)
    if artifact is None:
        return result
    await db.scalp_models.update_one({"model_key": key}, {"$set": artifact},
                                     upsert=True)
    # selection-bias audit trail: EVERY candidate is kept, rejected included
    await db.scalp_model_history.insert_one(dict(artifact))
    if artifact["usable"]:
        _active[key] = _to_runtime(artifact)
    else:
        _active.pop(key, None)
    logger.info("scalp model %s n=%d auc=%.3f brier=%.4f/%.4f netexp=%s usable=%s",
                key, artifact["n_samples"], artifact["oos_auc"], artifact["brier"],
                artifact["brier_baseline"], artifact["oos_net_expectancy_pips"],
                artifact["usable"])
    return result


def _train_from_docs(docs: list, key: str, symbol: str, broker: str,
                     account_type: str):
    """Pure CPU part of the retrain (numpy) — runs in a worker thread.
    Returns (result_dict, artifact_or_None)."""
    X, res, net, tgt, stp, cost, ts = [], [], [], [], [], [], []
    for d in docs:
        # feature contract: only train on decisions produced under the
        # CURRENT schema (absent stamp → v1, the original feature set)
        if int(d.get("feature_schema_version") or 1) != FEATURE_SCHEMA_VERSION:
            continue
        v = vectorize(d.get("features") or {},
                      d.get("feature_schema_version"))
        if v is None:
            continue
        fc = d.get("forecast") or {}
        X.append(v)
        res.append(d["outcome"]["result"])
        net.append(float(d["outcome"].get("net_pips") or 0))
        tgt.append(float(fc.get("target_pips") or 2.0))
        stp.append(float(fc.get("stop_pips") or 2.0))
        cost.append(float(d.get("cost_pips") or 1.0))
        ts.append(int(d.get("ts_ms") or 0))
    n = len(res)
    X = np.array(X) if X else np.zeros((0, len(FEATURE_KEYS)))
    net, tgt, stp, cost = map(np.array, (net, tgt, stp, cost))
    ts = np.array(ts, dtype=np.int64)
    res = np.array(res)
    dir_mask = res != "timeout"
    y = (res == "target_first").astype(float)
    n_dir = int(dir_mask.sum())
    if n_dir < MIN_SAMPLES or len(set(y[dir_mask].tolist())) < 2:
        return {"trained": False, "model_key": key, "n": n, "n_directional": n_dir,
                "reason": f"insufficient directional samples ({n_dir}/{MIN_SAMPLES})"}, None

    # ---- chronological split over the FULL timeline (purged + embargoed) ----
    i_cal = max(50, int(n * 0.5))
    i_eval = int(n * 0.7)
    m_train = dir_mask.copy(); m_train[i_cal:] = False
    m_cal = dir_mask.copy(); m_cal[:i_cal] = False; m_cal[i_eval:] = False
    m_evald = dir_mask.copy(); m_evald[:i_eval] = False
    m_train = _purge(m_train, ts, i_cal)
    m_cal = _purge(m_cal, ts, i_eval)
    if (len(set(y[m_train].tolist())) < 2 or m_cal.sum() < 30
            or m_evald.sum() < 30):
        return {"trained": False, "model_key": key, "n": n, "n_directional": n_dir,
                "reason": "insufficient class balance / cal / eval windows"}, None

    # deployed base model = 50% train block; calibrator fit on ITS outputs
    w, mu, sd = _fit(X[m_train], y[m_train])
    a, b = fit_platt(_predict(w, mu, sd, X[m_cal]), y[m_cal])

    # untouched final evaluation with the EXACT deployed model + calibrator
    p_eval = apply_platt(_predict(w, mu, sd, X[m_evald]), a, b)
    y_eval = y[m_evald]
    oos_auc = round(_auc(y_eval, p_eval), 4)
    b_model = round(brier(p_eval, y_eval), 4)
    b_base = round(brier(np.full_like(y_eval, y[m_train | m_cal].mean()), y_eval), 4)
    ece_v = round(ece(p_eval, y_eval), 4)

    # ---- profitability gates over ALL eval-window outcomes incl. TIMEOUTS ----
    m_eval_all = np.zeros(n, dtype=bool); m_eval_all[i_eval:] = True
    p_all = apply_platt(_predict(w, mu, sd, X[m_eval_all]), a, b)
    sel = _trade_selected(p_all, tgt[m_eval_all], stp[m_eval_all], cost[m_eval_all])
    net_eval = net[m_eval_all]
    if sel.sum() >= MIN_SELECTED_TRADES:
        chosen = net_eval[sel]
        oos_net_exp = round(float(chosen.mean()), 3)
        # item 9 — a positive AVERAGE with a negative lower bound must fail
        net_exp_lb = round(_lower_bound(chosen), 3)
        stressed = chosen - (COST_STRESS_MULT - 1.0) * cost[m_eval_all][sel]
        stressed_exp = round(float(stressed.mean()), 3)
        stressed_lb = round(_lower_bound(stressed), 3)
        wins = float(chosen[chosen > 0].sum())
        losses = abs(float(chosen[chosen < 0].sum()))
        profit_factor = round(wins / losses, 2) if losses > 0 else None
        k = max(3, int(sel.sum() // 25))
        idx = np.where(sel)[0]
        windows = np.array_split(net_eval[idx], min(4, k))
        prof_ratio = round(float(np.mean([wd.mean() > 0 for wd in windows
                                          if len(wd)])), 2)
    else:
        oos_net_exp = net_exp_lb = stressed_exp = stressed_lb = prof_ratio = None
        profit_factor = None

    rolling_ratio = _rolling_positive_ratio(X, y, res, net, tgt, stp, cost, n,
                                            ts=ts)

    usable = bool(
        oos_auc >= MIN_OOS_AUC
        and b_model <= b_base
        and net_exp_lb is not None and net_exp_lb > 0
        and stressed_lb is not None and stressed_lb > 0
        and prof_ratio is not None and prof_ratio >= MIN_PROFITABLE_WINDOWS
        and rolling_ratio is not None and rolling_ratio >= MIN_ROLLING_POSITIVE
    )

    expires = (datetime.now(timezone.utc) + timedelta(days=MODEL_TTL_DAYS)).isoformat()
    artifact = {
        "model_key": key, "symbol": symbol.upper(),
        "broker": broker, "account_type": account_type,
        "n_samples": int(n), "n_directional": n_dir,
        "weights": w.tolist(), "mu": mu.tolist(), "sd": sd.tolist(),
        "platt_a": a, "platt_b": b,
        "calibrator_matches_deployed_model": True,   # Option C invariant
        "oos_auc": oos_auc, "brier": b_model, "brier_baseline": b_base,
        "ece": ece_v,
        "oos_net_expectancy_pips": oos_net_exp,
        "net_expectancy_lower_bound_pips": net_exp_lb,
        "stressed_net_expectancy_pips": stressed_exp,
        "stressed_lower_bound_pips": stressed_lb,
        "rolling_positive_ratio": rolling_ratio,
        "selected_eval_trades": int(sel.sum()),
        "profit_factor": profit_factor,
        "profitable_windows_ratio": prof_ratio,
        "timeouts_in_eval": int((res[m_eval_all] == "timeout").sum()),
        "usable": usable,
        "feature_keys": FEATURE_KEYS,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires,
    }
    result = {"trained": True, "model_key": key, "n": n, "n_directional": n_dir,
              "oos_auc": oos_auc,
              "brier": b_model, "brier_baseline": b_base, "ece": ece_v,
              "oos_net_expectancy_pips": oos_net_exp,
              "net_expectancy_lower_bound_pips": net_exp_lb,
              "stressed_net_expectancy_pips": stressed_exp,
              "stressed_lower_bound_pips": stressed_lb,
              "rolling_positive_ratio": rolling_ratio,
              "selected_eval_trades": int(sel.sum()),
              "profitable_windows_ratio": prof_ratio, "usable": usable}
    return result, artifact


def _to_runtime(artifact: dict) -> dict:
    return {"w": np.array(artifact["weights"]), "mu": np.array(artifact["mu"]),
            "sd": np.array(artifact["sd"]),
            "platt_a": artifact["platt_a"], "platt_b": artifact["platt_b"],
            "expires_at": artifact["expires_at"], "oos_auc": artifact["oos_auc"],
            "trained_at": artifact.get("trained_at"),
            "feature_schema_version": artifact.get("feature_schema_version", 1),
            "ts": time.time()}


def version_of(model_key: str) -> dict | None:
    """Model + calibration provenance for decision stamping (refinement 4).
    The Platt calibrator is trained WITH the model (Option C invariant), so
    trained_at identifies both."""
    rt = _active.get(model_key)
    if not rt:
        return None
    return {"trained_at": rt.get("trained_at"),
            "feature_schema_version": rt.get("feature_schema_version", 1),
            "calibration": "platt", "oos_auc": rt.get("oos_auc")}


def predict(model_key: str, features: dict) -> dict:
    """Structured prediction (review item 10).

    Returns {"p": float|None, "source": "model"|"deterministic",
             "fallback_reason": str|None}."""
    ent = _active.get(model_key)
    if ent is None:
        return {"p": None, "source": "deterministic", "fallback_reason": "no_model"}
    try:
        if datetime.fromisoformat(ent["expires_at"]) < datetime.now(timezone.utc):
            _active.pop(model_key, None)
            return {"p": None, "source": "deterministic",
                    "fallback_reason": "model_expired"}
    except (ValueError, TypeError):
        return {"p": None, "source": "deterministic",
                "fallback_reason": "invalid_expiry"}
    v = vectorize(features)
    if v is None:
        return {"p": None, "source": "deterministic",
                "fallback_reason": "invalid_features"}
    x = np.array(v)
    z = np.abs((x - ent["mu"]) / ent["sd"])
    if float(z.max()) > OOD_Z_LIMIT:
        return {"p": None, "source": "deterministic",
                "fallback_reason": "ood_features"}
    p_raw = _predict(ent["w"], ent["mu"], ent["sd"], x.reshape(1, -1))[0]
    p = float(apply_platt(np.array([p_raw]), ent["platt_a"], ent["platt_b"])[0])
    return {"p": p, "source": "model", "fallback_reason": None}


def predict_p(model_key: str, features: dict) -> float | None:
    """Calibrated probability, or None → deterministic baseline keeps control."""
    return predict(model_key, features)["p"]


async def load_persisted(db, model_key: str) -> bool:
    doc = await db.scalp_models.find_one({"model_key": model_key, "usable": True})
    if not doc:
        return False
    # feature contract: a model trained under a different schema must never
    # serve predictions against current feature vectors
    if int(doc.get("feature_schema_version") or 1) != FEATURE_SCHEMA_VERSION:
        return False
    try:
        if datetime.fromisoformat(doc["expires_at"]) < datetime.now(timezone.utc):
            return False
    except (KeyError, ValueError, TypeError):
        return False
    _active[model_key] = _to_runtime(doc)
    return True
