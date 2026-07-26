"""iter-108 · Stacked ML Ensemble — 7 models, intelligently averaged.

Members:
  Trained on the user's real closed trades (skill-weighted):
    · Gradient Boosting (sklearn) · XGBoost · LightGBM · CatBoost
  Live agents (evidence-weighted):
    · Transformer (Chronos forecast band) · RL agent · Bayesian model

Averaging is EARNED, not equal: GBM weights come from walk-forward
out-of-sample AUC (weight = AUC − 0.5, zero if no proven skill), scaled by
sample size; RL/Bayes weights scale with their observed-trade evidence; the
Transformer gets a fixed modest weight. Final p_win = Σ wᵢpᵢ / Σ wᵢ."""
import logging
import math
from datetime import datetime, timezone
from pathlib import Path

from pip_utils import price_to_pips
from rl_policy import _session_of, _regime_of

logger = logging.getLogger(__name__)

MODEL_DIR = Path("/app/backend/models_store")
MIN_TRADES = 40
MODEL_TTL_HOURS = 12
LOOKBACK_DAYS = 120
GBM_BLOCK_WEIGHT = 0.7      # max share of the vote the 4 GBMs can earn
ML_GATE_P = 0.40

FEATURE_NAMES = [
    "action_buy", "confidence", "rsi_short", "slope_short",
    "tier_short", "tier_medium", "tier_long", "align",
    "sess_asian", "sess_london", "sess_newyork",
    "regime_trend", "regime_range", "regime_volatile",
    "hour_sin", "hour_cos", "dow", "sl_pips", "rr",
]

_models_cache: dict = {}    # {uid: (mtime, {name: model})}


def featurize(action, symbol, sig, when=None) -> list:
    sig = sig or {}
    when = when or datetime.now(timezone.utc)
    tiers = sig.get("mtf_tiers") or {}

    def tdir(k):
        d = (tiers.get(k) or {}).get("direction")
        return 1.0 if d == "UP" else -1.0 if d == "DOWN" else 0.0

    ts, tm, tl = tdir("SHORT"), tdir("MEDIUM"), tdir("LONG")
    adir = 1.0 if action == "BUY" else -1.0
    align = (int(ts == adir) + int(tm == adir) + int(tl == adir)) / 3.0
    sess = _session_of(sig)
    reg = _regime_of(sig)
    sh = tiers.get("SHORT") or {}
    entry, sl = sig.get("entry_price"), sig.get("stop_loss")
    tp = sig.get("tp1") or sig.get("take_profit")
    sl_pips = rr = 0.0
    try:
        if entry and sl:
            sl_pips = abs(price_to_pips(symbol, float(entry) - float(sl)))
            if tp and sl_pips > 0:
                rr = abs(price_to_pips(symbol, float(tp) - float(entry))) / sl_pips
    except Exception:
        pass
    return [
        1.0 if action == "BUY" else 0.0,
        float(sig.get("confidence") or 50),
        float(sh.get("rsi") or 50),
        float(sh.get("sma_fast_slope_pct") or 0),
        ts, tm, tl, align,
        1.0 if "asia" in sess else 0.0,
        1.0 if "london" in sess else 0.0,
        1.0 if ("new" in sess or "ny" in sess) else 0.0,
        1.0 if "TREND" in reg else 0.0,
        1.0 if "RANGE" in reg else 0.0,
        1.0 if "VOL" in reg else 0.0,
        math.sin(2 * math.pi * when.hour / 24),
        math.cos(2 * math.pi * when.hour / 24),
        float(when.weekday()),
        min(sl_pips, 2000.0), min(rr, 10.0),
    ]


def _gbm_zoo():
    from sklearn.ensemble import GradientBoostingClassifier
    from xgboost import XGBClassifier
    from lightgbm import LGBMClassifier
    from catboost import CatBoostClassifier
    common = dict(n_estimators=80, max_depth=3, learning_rate=0.08)
    return {
        "gradient_boosting": GradientBoostingClassifier(random_state=7, **common),
        "xgboost": XGBClassifier(random_state=7, eval_metric="logloss",
                                 verbosity=0, **common),
        "lightgbm": LGBMClassifier(random_state=7, verbose=-1, **common),
        "catboost": CatBoostClassifier(iterations=80, depth=3, learning_rate=0.08,
                                       verbose=0, random_seed=7,
                                       allow_writing_files=False),
    }


def train_sync(X: list, y: list, uid: str) -> dict:
    """Walk-forward CV per GBM → out-of-sample AUC → skill weights, then
    refit on everything and persist. Runs in a worker thread."""
    import joblib
    import numpy as np
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import TimeSeriesSplit

    X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=int)
    fold_aucs = {name: [] for name in _gbm_zoo()}
    for tr, va in TimeSeriesSplit(n_splits=3).split(X):
        if len(set(y[tr])) < 2 or len(set(y[va])) < 2:
            continue
        for name, mdl in _gbm_zoo().items():
            try:
                mdl.fit(X[tr], y[tr])
                fold_aucs[name].append(
                    float(roc_auc_score(y[va], mdl.predict_proba(X[va])[:, 1])))
            except Exception as e:
                logger.debug("ensemble CV %s failed: %s", name, e)
    aucs = {k: round(sum(v) / len(v), 3) if v else 0.5
            for k, v in fold_aucs.items()}
    skills = {k: max(0.0, a - 0.5) for k, a in aucs.items()}
    total = sum(skills.values())
    weights = {k: round(skills[k] / total, 3) if total > 0 else 0.0
               for k in skills}
    final = {}
    for name, mdl in _gbm_zoo().items():
        try:
            mdl.fit(X, y)
            final[name] = mdl
        except Exception as e:
            logger.debug("ensemble final fit %s failed: %s", name, e)
    path = MODEL_DIR / uid
    path.mkdir(parents=True, exist_ok=True)
    joblib.dump(final, path / "gbm_ensemble.joblib")
    _models_cache.pop(uid, None)
    return {"aucs": aucs, "weights": weights, "models_saved": len(final)}


async def train_ensemble(db, user_id: str) -> dict:
    import asyncio
    from bson import ObjectId
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    trades = await db.trades.find({
        "user_id": user_id, "status": "closed", "pnl": {"$ne": None},
        "origin": "auto", "closed_at": {"$gte": since},
        "pnl_estimated": {"$ne": True}, "pnl_unknown": {"$ne": True},
    }).sort("closed_at", 1).to_list(5000)
    meta = {"user_id": user_id, "n_trades": len(trades),
            "feature_names": FEATURE_NAMES,
            "trained_at": datetime.now(timezone.utc).isoformat()}
    if len(trades) < MIN_TRADES:
        meta["status"] = "insufficient_data"
        await db.ml_ensembles.update_one({"user_id": user_id},
                                         {"$set": meta}, upsert=True)
        return meta
    sids = []
    for t in trades:
        try:
            sids.append(ObjectId(t["signal_id"]))
        except Exception:
            continue
    sigs = {}
    if sids:
        async for s in db.signals.find(
                {"_id": {"$in": sids}},
                {"session": 1, "regime": 1, "mtf_tiers": 1, "stop_loss": 1,
                 "confidence": 1, "entry_price": 1, "tp1": 1, "take_profit": 1}):
            sigs[str(s["_id"])] = s
    X, y = [], []
    for t in trades:
        if not t.get("pnl"):
            continue
        sig = sigs.get(str(t.get("signal_id") or "")) or {}
        sig = {**sig, "entry_price": t.get("entry_price") or sig.get("entry_price"),
               "stop_loss": t.get("stop_loss") or sig.get("stop_loss")}
        when = None
        try:
            when = datetime.fromisoformat(
                str(t.get("opened_at") or t.get("created_at")))
        except (ValueError, TypeError):
            pass
        X.append(featurize(t.get("action"), t.get("symbol"), sig, when))
        y.append(1 if t["pnl"] > 0 else 0)
    result = await asyncio.to_thread(train_sync, X, y, user_id)
    meta.update(status="trained", **result)
    await db.ml_ensembles.update_one({"user_id": user_id},
                                     {"$set": meta}, upsert=True)
    return meta


async def get_meta(db, user_id: str) -> dict:
    doc = await db.ml_ensembles.find_one({"user_id": user_id})
    if doc:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(doc["trained_at"])).total_seconds()
            if age < MODEL_TTL_HOURS * 3600:
                return doc
        except (KeyError, ValueError):
            pass
    return await train_ensemble(db, user_id)


def _load_models(uid: str) -> dict:
    import joblib
    f = MODEL_DIR / uid / "gbm_ensemble.joblib"
    if not f.exists():
        return {}
    mtime = f.stat().st_mtime
    hit = _models_cache.get(uid)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        models = joblib.load(f)
    except Exception as e:
        logger.warning("ensemble model load failed: %s", e)
        return {}
    _models_cache[uid] = (mtime, models)
    return models


def _transformer_member(fc: dict | None, action: str) -> float | None:
    if not fc or fc.get("q50") is None or not fc.get("last"):
        return None
    last = fc["last"]
    band_up = fc.get("q10", last) > last
    band_dn = fc.get("q90", last) < last
    med_up = fc["q50"] > last
    if action == "BUY":
        return 0.75 if band_up else 0.25 if band_dn else (0.6 if med_up else 0.4)
    return 0.75 if band_dn else 0.25 if band_up else (0.4 if med_up else 0.6)


def blend(members: list) -> float | None:
    tw = sum(m["w"] for m in members)
    if tw <= 0:
        return None
    return sum(m["p"] * m["w"] for m in members) / tw


async def ml_predict(db, user_id: str, signal: dict, symbol: str) -> dict:
    # Deployment kill-switch — set ML_ENSEMBLE_ENABLED=false on
    # memory-constrained pods to skip loading the GBM zoo (xgboost/
    # lightgbm/catboost). Returns a neutral prediction (no veto). Default
    # enabled.
    import os
    if os.environ.get("ML_ENSEMBLE_ENABLED", "true").lower() != "true":
        return {"p_win": None, "models_used": 0, "members": []}
    meta = await get_meta(db, user_id)
    members = []
    if meta.get("status") == "trained":
        models = _load_models(user_id)
        if models:
            x = featurize(signal.get("action"), symbol, signal)
            scale = min((meta.get("n_trades") or 0) / 150.0, 1.0) * GBM_BLOCK_WEIGHT
            for name, mdl in models.items():
                w = float((meta.get("weights") or {}).get(name) or 0) * scale
                if w <= 0:
                    continue
                try:
                    p = float(mdl.predict_proba([x])[0][1])
                except Exception as e:
                    logger.debug("ensemble predict %s failed: %s", name, e)
                    continue
                members.append({"name": name, "p": round(p, 3), "w": round(w, 3)})
    tp = _transformer_member(signal.get("forecast"), signal.get("action"))
    if tp is not None:
        members.append({"name": "transformer", "p": round(tp, 3), "w": 0.25})
    rl = signal.get("rl_policy") or {}
    if rl.get("n"):
        p = 1.0 / (1.0 + math.exp(-float(rl.get("mean") or 0) / 40.0))
        members.append({"name": "rl_agent", "p": round(p, 3),
                        "w": round(0.35 * min(rl["n"] / 20.0, 1.0), 3)})
    bd = signal.get("bayes") or {}
    if bd.get("n"):
        members.append({"name": "bayesian", "p": float(bd["p_success"]),
                        "w": round(0.35 * min(bd["n"] / 20.0, 1.0), 3)})
    p_win = blend(members)
    return {"p_win": round(p_win, 3) if p_win is not None else None,
            "members": members, "models_used": len(members),
            "gbm_status": meta.get("status"),
            "trained_n": meta.get("n_trades"),
            "gbm_auc": meta.get("aucs")}


def ml_gate(pred: dict | None) -> str | None:
    """Veto when the full ensemble agrees the trade probably loses."""
    if not pred or pred.get("p_win") is None or pred.get("models_used", 0) < 3:
        return None
    if pred["p_win"] < ML_GATE_P:
        names = ", ".join(m["name"] for m in pred["members"])
        return (f"ML ensemble gate: {pred['models_used']} models "
                f"({names}) average P(win) {pred['p_win']:.0%} — below "
                f"{ML_GATE_P:.0%}, trade vetoed.")
    return None
