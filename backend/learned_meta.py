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

def _xgb():
    """iter-173 — lazy, memory-gated XGBoost import. The old module-level
    import loaded xgboost+sklearn into EVERY API boot (~400MB), OOM-killing
    1Gi production pods. Returns the module or None."""
    from ml_runtime import ml_runtime_enabled
    if not ml_runtime_enabled():
        return None
    try:
        import xgboost
        return xgboost
    except ImportError:  # pragma: no cover — only when xgboost not installed
        return None

from database import get_db
from probability_calibrator import (fit_platt, apply_platt, brier_score,
                                    expected_calibration_error,
                                    holdout_tail_indices, platt_is_valid)

logger = logging.getLogger("learned_meta")

MIN_SAMPLES = 30                    # global minimum to train at all
MIN_SAMPLES_PER_SESSION = 25        # per-session minimum to train a session-specific model
MIN_SAMPLES_XGB = 100               # iter-69 · XGBoost only kicks in above this sample count
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

    X, y, sessions, pnls, entered = [], [], [], [], []
    quality = {"total_closed": len(trades), "accepted": 0,
               "alpha_clean_accepted": 0, "unattributed_included": 0,
               "excluded_noise": 0, "excluded_by_category": {}}
    for t in trades:
        # Alpha-clean gate — outcomes dominated by broker/execution/infra/
        # news noise NEVER retrain the strategy (Outcome Attribution v56).
        if t.get("alpha_clean") is False:
            quality["excluded_noise"] += 1
            cat = str(t.get("attribution_primary") or "NOISE")
            quality["excluded_by_category"][cat] = \
                quality["excluded_by_category"].get(cat, 0) + 1
            continue
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
        entered.append(str(entered_at or ""))
        X.append(feats)
        y.append(1 if float(t.get("pnl") or 0) > 0 else 0)
        pnls.append(abs(float(t.get("pnl") or 0)))
        quality["accepted"] += 1
        if t.get("alpha_clean") is True:
            quality["alpha_clean_accepted"] += 1
        else:
            quality["unattributed_included"] += 1

    # H5 · chronological order — walk-forward OOS calibration requires it
    if X:
        order = sorted(range(len(X)), key=lambda i: entered[i])
        X = [X[i] for i in order]
        y = [y[i] for i in order]
        sessions = [sessions[i] for i in order]
        pnls = [pnls[i] for i in order]

    # iter-42 · Profit-weighted samples — win rate and profit tied. Each
    # trade's training weight scales with |pnl| (normalised to the median),
    # so a -$362 loss teaches the model ~4× more than a -$50 scratch and a
    # big winner counts more than a tiny one. Clipped to [0.25, 4.0].
    if pnls:
        arr = np.array(pnls, dtype=float)
        nonzero = arr[arr > 0]
        med = float(np.median(nonzero)) if nonzero.size else 1.0
        sample_w = np.clip(arr / max(med, 1e-9), 0.25, 4.0)
    else:
        sample_w = np.array([])
    return (np.array(X, dtype=float), np.array(y, dtype=float), sessions,
            sample_w, quality)


def _train_logreg(X: np.ndarray, y: np.ndarray,
                  sample_w: np.ndarray | None = None) -> tuple[np.ndarray, float]:
    """Plain L2-regularised logistic regression with GD (optionally
    profit-weighted). Returns (weights_with_bias, train_auc)."""
    n, d = X.shape
    # Standardise — mean/std per column, store for inference
    mu = X.mean(axis=0)
    sd = X.std(axis=0) + 1e-9
    Xn = (X - mu) / sd
    Xb = np.hstack([Xn, np.ones((n, 1))])   # add bias column

    if sample_w is None or len(sample_w) != n:
        sw = np.ones(n)
    else:
        sw = sample_w / max(float(sample_w.mean()), 1e-9)  # mean-normalise

    w = np.zeros(d + 1)
    for _ in range(EPOCHS):
        z = Xb @ w
        p = _sigmoid(z)
        grad = (Xb.T @ ((p - y) * sw)) / n + L2 * np.concatenate([w[:-1], [0]])
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


def _train_xgb(X: np.ndarray, y: np.ndarray,
               sample_w: np.ndarray | None = None) -> tuple[bytes, np.ndarray, float]:
    """Train a small XGBoost classifier (iter-69, profit-weighted iter-42).
    Returns (model_bytes, p_train, train_auc).

    Uses the low-level `xgb.train` API (no sklearn dependency). Capacity is
    deliberately small — the dataset is small (100-5000 rows) and we want to
    avoid overfitting.
    """
    n = len(y)
    pos = float((y == 1).sum())
    neg = float((y == 0).sum())
    spw = (neg / pos) if pos > 0 else 1.0

    xgb = _xgb()
    if xgb is None:
        raise RuntimeError("xgboost unavailable (heavy ML disabled)")
    if sample_w is not None and len(sample_w) == n:
        dmat = xgb.DMatrix(X, label=y, weight=sample_w)
    else:
        dmat = xgb.DMatrix(X, label=y)
    params = {
        "objective": "binary:logistic",
        "max_depth": 3,
        "eta": 0.08,
        "subsample": 0.85,
        "colsample_bytree": 0.85,
        "reg_lambda": 1.0,
        "scale_pos_weight": spw,
        "tree_method": "hist",
        "eval_metric": "logloss",
        "verbosity": 0,
        "nthread": 1,
    }
    booster = xgb.train(params, dmat, num_boost_round=120)
    p = booster.predict(dmat)
    auc = _auc(y, p) if len(set(y.tolist())) == 2 else 0.5

    raw = booster.save_raw(raw_format="json")
    if isinstance(raw, bytearray):
        raw = bytes(raw)
    return raw, p, auc


def _xgb_predict_proba(model_bytes: bytes, X: np.ndarray) -> np.ndarray:
    """Reconstitute a saved XGBoost booster and predict probabilities."""
    xgb = _xgb()
    if xgb is None:
        raise RuntimeError("xgboost unavailable (heavy ML disabled)")
    booster = xgb.Booster()
    booster.load_model(bytearray(model_bytes))
    dmat = xgb.DMatrix(X)
    return booster.predict(dmat)


def _walk_forward_oos(X: np.ndarray, y: np.ndarray,
                      sample_w: np.ndarray | None, use_xgb: bool,
                      n_folds: int = 4, min_train: int = 20):
    """H5 · Expanding-window walk-forward: train on trades [0, lo), predict
    fold [lo, hi). Returns (indices, oos_predictions) or (None, None) when
    the dataset is too small. Data MUST be in chronological order."""
    n = len(y)
    start = max(min_train, int(n * 0.4))
    if n - start < 10:
        return None, None
    bounds = np.linspace(start, n, n_folds + 1).astype(int)
    idx, preds = [], []
    for k in range(n_folds):
        lo, hi = int(bounds[k]), int(bounds[k + 1])
        if hi <= lo:
            continue
        ytr = y[:lo]
        if len(set(ytr.tolist())) < 2:
            continue
        swtr = sample_w[:lo] if (sample_w is not None and len(sample_w) == n) else None
        if use_xgb:
            mb, _, _ = _train_xgb(X[:lo], ytr, swtr)
            p = _xgb_predict_proba(mb, X[lo:hi])
        else:
            w, mu, sd, _ = _train_logreg(X[:lo], ytr, swtr)
            Xn = np.hstack([(X[lo:hi] - mu) / sd, np.ones((hi - lo, 1))])
            p = _sigmoid(Xn @ w)
        idx.extend(range(lo, hi))
        preds.extend(np.asarray(p).ravel().tolist())
    if len(preds) < 10:
        return None, None
    return np.array(idx, dtype=int), np.array(preds, dtype=float)


def _fit_artifact(X: np.ndarray, y: np.ndarray, key: str, label: str,
                  sample_w: np.ndarray | None = None) -> dict:
    """Train one classifier on (X, y) and return the persistable artifact dict.

    iter-69 · For samples ≥100 we use XGBoost; below that we fall back to
    L2-regularised logistic regression so the model still trains on small
    per-session slices. Both paths feed through identical Platt calibration.
    iter-42 · `sample_w` profit-weights every example so P(win) is trained
    to care about the SIZE of wins/losses, not just their count.
    """
    n = len(y)
    use_xgb = _xgb() is not None and n >= MIN_SAMPLES_XGB

    if use_xgb:
        model_bytes, p_train, auc = _train_xgb(X, y, sample_w)
        backend = "xgboost"
        # XGBoost takes raw features — no standardisation needed.
        model_payload = {
            "xgb_model_b64": _b64encode_bytes(model_bytes),
        }
        p_final = p_train
    else:
        w, mu, sd, auc = _train_logreg(X, y, sample_w)
        backend = "logreg"
        p_final = _sigmoid(
            np.hstack([(X - mu) / sd, np.ones((n, 1))]) @ w
        )
        model_payload = {
            "weights": w.tolist(),
            "mu": mu.tolist(),
            "sd": sd.tolist(),
        }

    # H5 · quant review: threshold + Platt calibration must be fit on
    # walk-forward OUT-OF-SAMPLE predictions, never on the training fit —
    # in-sample calibration is systematically overconfident. The deployed
    # model still trains on ALL data; only evaluation/calibration is OOS.
    oos_idx, oos_p = _walk_forward_oos(X, y, sample_w, use_xgb)
    if oos_p is not None and len(set(y[oos_idx].tolist())) == 2:
        p_eval, y_eval = oos_p, y[oos_idx]
        cal_source = "walk_forward_oos"
        oos_auc = round(_auc(y_eval, p_eval), 4)
    else:
        # iter-191 · TRUE held-out (chronological tail) once n ≥ 100 —
        # resolves the long-standing calibrator TODO. Model is refit on the
        # head only so tail scores are genuinely out-of-sample.
        tail = holdout_tail_indices(n)
        head_ok = (tail is not None
                   and len(set(y[:tail[0]].tolist())) == 2)
        if head_ok:
            lo = int(tail[0])
            swh = sample_w[:lo] if (sample_w is not None
                                    and len(sample_w) == n) else None
            if use_xgb:
                mb, _, _ = _train_xgb(X[:lo], y[:lo], swh)
                p_eval = _xgb_predict_proba(mb, X[tail])
            else:
                wh, muh, sdh, _ = _train_logreg(X[:lo], y[:lo], swh)
                p_eval = _sigmoid(np.hstack(
                    [(X[tail] - muh) / sdh,
                     np.ones((len(tail), 1))]) @ wh)
            y_eval = y[tail]
            cal_source = "holdout_tail"
            oos_auc = (round(_auc(y_eval, p_eval), 4)
                       if len(set(y_eval.tolist())) == 2 else None)
        else:
            p_eval, y_eval = p_final, y
            cal_source = "in_sample_fallback"
            oos_auc = None

    # Calibrate threshold: lowest p_win below which precision_of_rejection >= 0.6
    threshold = 0.45
    for cand in np.arange(0.30, 0.50, 0.01):
        rejected = p_eval < cand
        if rejected.sum() == 0:
            continue
        if (y_eval[rejected] == 0).mean() >= 0.6:
            threshold = float(cand)
            break

    # iter-52 · Platt scaling for calibrated probabilities (H5: fit on OOS).
    brier_raw = brier_score(p_eval, y_eval)
    platt = fit_platt(p_eval, y_eval)
    if not platt.get("skipped"):
        p_cal = np.array([apply_platt(float(pi), platt) for pi in p_eval])
        brier_cal = brier_score(p_cal, y_eval)
        platt["brier_raw"] = round(brier_raw, 4)
        platt["brier_calibrated"] = round(brier_cal, 4)
        platt["ece_raw"] = expected_calibration_error(p_eval, y_eval)
        platt["ece_calibrated"] = expected_calibration_error(p_cal, y_eval)

    return {
        "key": key,
        "label": label,
        "backend": backend,
        **model_payload,
        "threshold": threshold,
        "train_auc": round(auc, 4),
        "oos_auc": oos_auc,
        "calibration_source": cal_source,
        "n_samples": int(n),
        "n_wins": int(y.sum()),
        "profit_weighted": bool(sample_w is not None and len(sample_w) == n),
        "calibration": platt,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "feature_names": [
            "confidence_norm", "is_buy", "kalman_vel_norm",
            "cot_against", "cot_with", "tips_aligned",
            "mtf_aligned", "macro_event_24h",
        ],
    }


def _b64encode_bytes(b: bytes) -> str:
    import base64
    return base64.b64encode(b).decode("ascii")


def _b64decode_bytes(s: str) -> bytes:
    import base64
    return base64.b64decode(s.encode("ascii"))


async def _record_calibration_history(db, doc: dict) -> None:
    """iter-191 · track calibration error over time (review v53 §18)."""
    cal = doc.get("calibration") or {}
    await db.calibration_history.insert_one(
        {"key": doc["key"], "at": doc["trained_at"],
         "source": doc.get("calibration_source"),
         "n": cal.get("n"), "skipped": bool(cal.get("skipped")),
         "brier_raw": cal.get("brier_raw"),
         "brier_calibrated": cal.get("brier_calibrated"),
         "ece_raw": cal.get("ece_raw"),
         "ece_calibrated": cal.get("ece_calibrated"),
         "oos_auc": doc.get("oos_auc")})


async def retrain() -> dict:
    """Pull closed-trade dataset, train global + per-session models. Persist artifacts."""
    db = get_db()
    # attribute any straggler closed trades first so the alpha-clean gate
    # sees an attribution verdict for (nearly) every candidate sample
    try:
        from outcome_attribution import attribute_missing
        await attribute_missing(db, limit=500)
    except Exception as e:
        logger.warning("pre-train attribution backfill failed: %s", e)
    X, y, sessions, sample_w, quality = await _build_dataset()
    n = len(y)
    await db.learning_quality.replace_one(
        {"_id": "last"},
        {"_id": "last", "at": datetime.now(timezone.utc).isoformat(),
         **quality, "n_samples": n, "trained": n >= MIN_SAMPLES},
        upsert=True)
    if n < MIN_SAMPLES:
        return {
            "trained": False,
            "reason": f"need ≥{MIN_SAMPLES} closed trades (have {n})",
            "n_samples": n,
            "learning_quality": quality,
        }

    # 1. Global fallback artifact — always trained when there's enough data
    global_doc = _fit_artifact(X, y, ARTIFACT_KEY, "GLOBAL", sample_w)
    await db.learned_meta_artifacts.update_one(
        {"key": ARTIFACT_KEY}, {"$set": global_doc}, upsert=True
    )
    await _record_calibration_history(db, global_doc)

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
            doc = _fit_artifact(X[mask], y[mask], key, label, sample_w[mask])
            await db.learned_meta_artifacts.update_one(
                {"key": key}, {"$set": doc}, upsert=True
            )
            await _record_calibration_history(db, doc)
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
        # Back-compat top-level keys (mirror of `global` for legacy consumers)
        **{k: global_doc[k] for k in ("n_samples", "n_wins", "train_auc", "threshold", "trained_at")},
        "global": {k: global_doc[k] for k in ("n_samples", "n_wins", "train_auc", "threshold", "trained_at")},
        "per_session": per_session,
        "learning_quality": quality,
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
    otherwise falls back to the global model. iter-69 · supports both
    `backend="xgboost"` and `backend="logreg"` artifacts.
    """
    current_session_label = session_bucket(datetime.now(timezone.utc))
    art = await get_artifact(session_label=current_session_label)
    if not art:
        return None
    feats = _features_from_signal(signal)
    if feats is None:
        return None
    try:
        x_row = np.array(feats, dtype=float).reshape(1, -1)
        backend = art.get("backend", "logreg")

        if backend == "xgboost" and _xgb() is not None:
            model_bytes = _b64decode_bytes(art["xgb_model_b64"])
            p = float(_xgb_predict_proba(model_bytes, x_row)[0])
        else:
            # Logistic regression fallback (works also for any legacy
            # iter-52..68 artifact without a `backend` field).
            w = np.array(art["weights"])
            mu = np.array(art["mu"])
            sd = np.array(art["sd"])
            x = (x_row[0] - mu) / sd
            xb = np.append(x, 1.0)
            p = float(_sigmoid(xb @ w))

        threshold = float(art["threshold"])
        calib = art.get("calibration") or {}
        calibrated = platt_is_valid(calib)   # C1: legacy inverted-convention artifacts are NOT applied
        p_cal = apply_platt(p, calib) if calibrated else p
        return {
            "p_win": round(p_cal, 4),
            "p_win_raw": round(p, 4),
            "p_win_calibrated": round(p_cal, 4),
            "calibrated": calibrated,
            "threshold": round(threshold, 4),
            "verdict": "REJECT" if p_cal < threshold else "ACCEPT",
            "n_samples": int(art.get("n_samples", 0)),
            "train_auc": float(art.get("train_auc", 0.5)),
            "brier_raw": (calib or {}).get("brier_raw"),
            "brier_calibrated": (calib or {}).get("brier_calibrated"),
            "backend": backend,
            "model_used": art.get("label", "GLOBAL"),
            "current_session": current_session_label,
        }
    except Exception as e:
        logger.warning("learned_meta inference failed: %s", e)
        return None
