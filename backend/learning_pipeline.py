"""Continuous-learning pipeline (Phase 5) — NO direct self-learning from
live trades. Every model update flows through the staged workflow:

    live trades → replay → shadow → validation → approval → production

  FREEZE GUARD   retraining is frozen after a short losing streak (≥5
                 consecutive losses inside 24h) or a tripped breaker —
                 the exact moment naive online learning would internalize
                 bad behavior.
  REPLAY         a CANDIDATE GBM ensemble is trained on the older 80% of
                 the trade history only (time-ordered split).
  SHADOW         the candidate predicts the held-out most-recent 20% it
                 has never seen — a true out-of-sample shadow run.
  VALIDATION     candidate holdout AUC must be non-inferior to the CURRENT
                 production model on the SAME holdout (tolerance 0.02).
  APPROVAL       non-inferior candidates are approved (recorded as
                 auto-approval); inferior candidates are REJECTED and
                 production stays untouched.
  PRODUCTION     only then is the standard full-data refit persisted.

The advisory count-based models (RL policy, Bayes) are freeze-gated and
version-snapshotted (`model_versions`, last 5) for instant rollback —
their outputs already pass through hard clamps downstream.

Engine-parameter learning already follows this pipeline via the Shadow Lab
(model_shadow.py + nightly_tuner.py + human-approved promotion).
Every run is recorded in `learning_runs` for the pipeline UI.
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("learning-pipeline")

STREAK_FREEZE_N = 5
FREEZE_HOURS = 24
HOLDOUT_FRAC = 0.2
MIN_HOLDOUT = 8
AUC_TOLERANCE = 0.02
VERSION_KEEP = 5


def _now():
    return datetime.now(timezone.utc)


# ------------------------------------------------------------ freeze guard
async def freeze_check(db, user_id: str) -> dict:
    """Learning freezes while the system is in a bad short-term state."""
    recent = await db.trades.find(
        {"user_id": user_id, "status": "closed", "origin": "auto",
         "pnl": {"$ne": None}},
        {"pnl": 1, "closed_at": 1}).sort("closed_at", -1).to_list(length=STREAK_FREEZE_N)
    if len(recent) >= STREAK_FREEZE_N and all(float(t["pnl"]) < 0 for t in recent):
        try:
            last = datetime.fromisoformat(str(recent[0]["closed_at"]))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if _now() - last < timedelta(hours=FREEZE_HOURS):
                return {"frozen": True,
                        "reason": f"{STREAK_FREEZE_N}-trade losing streak — "
                                  f"learning frozen for {FREEZE_HOURS}h so the "
                                  f"streak is not internalized"}
        except (ValueError, TypeError):
            pass
    since = (_now() - timedelta(hours=FREEZE_HOURS)).isoformat()
    tripped = await db.bot_configs.count_documents(
        {"user_id": user_id, "tripped_at": {"$gte": since}})
    if tripped:
        return {"frozen": True,
                "reason": "circuit breaker tripped in the last 24h — "
                          "learning frozen until conditions stabilize"}
    return {"frozen": False, "reason": "no losing streak, no tripped breakers"}


# ---------------------------------------------------- candidate evaluation
async def _build_xy(db, user_id: str, trades: list):
    from bson import ObjectId
    from ml_ensemble import featurize
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
        sig = {**sig,
               "entry_price": t.get("entry_price") or sig.get("entry_price"),
               "stop_loss": t.get("stop_loss") or sig.get("stop_loss")}
        when = None
        try:
            when = datetime.fromisoformat(
                str(t.get("opened_at") or t.get("created_at")))
        except (ValueError, TypeError):
            pass
        X.append(featurize(t.get("action"), t.get("symbol"), sig, when))
        y.append(1 if float(t["pnl"]) > 0 else 0)
    return X, y


def _holdout_auc_sync(X_train, y_train, X_hold, y_hold, uid: str) -> dict:
    """Train a candidate on the older split, score BOTH candidate and the
    current production ensemble on the never-seen holdout. Thread-safe."""
    import numpy as np
    from sklearn.metrics import roc_auc_score
    from ml_ensemble import _gbm_zoo, _load_models

    Xt, yt = np.asarray(X_train, float), np.asarray(y_train, int)
    Xh, yh = np.asarray(X_hold, float), np.asarray(y_hold, int)
    if len(set(yt)) < 2 or len(set(yh)) < 2:
        return {"status": "degenerate_labels"}

    def _score(models: dict) -> float | None:
        preds = []
        for mdl in models.values():
            try:
                preds.append(mdl.predict_proba(Xh)[:, 1])
            except Exception:
                continue
        if not preds:
            return None
        blended = np.mean(preds, axis=0)
        return float(roc_auc_score(yh, blended))

    candidate = {}
    for name, mdl in _gbm_zoo().items():
        try:
            mdl.fit(Xt, yt)
            candidate[name] = mdl
        except Exception:  # noqa: BLE001
            continue
    cand_auc = _score(candidate)
    prod_auc = _score(_load_models(uid) or {})
    return {"status": "ok", "candidate_auc": cand_auc,
            "production_auc": prod_auc, "holdout_n": int(len(yh))}


async def staged_ml_retrain(db, user_id: str) -> dict:
    """replay → shadow → validation → approval → production for the GBMs."""
    import asyncio
    from ml_ensemble import (LOOKBACK_DAYS, MIN_TRADES, ml_runtime_enabled,
                             train_ensemble)
    if not ml_runtime_enabled():
        return {"stage": "replay", "status": "disabled_low_memory"}
    since = (_now() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    trades = await db.trades.find({
        "user_id": user_id, "status": "closed", "pnl": {"$ne": None},
        "origin": "auto", "closed_at": {"$gte": since},
        "pnl_estimated": {"$ne": True}, "pnl_unknown": {"$ne": True},
    }).sort("closed_at", 1).to_list(5000)
    if len(trades) < MIN_TRADES:
        return {"stage": "replay", "status": "insufficient_data",
                "n_trades": len(trades)}
    # Fair holdout: prefer trades closed AFTER the production model was
    # trained — genuinely unseen by BOTH sides. If production is too fresh
    # for that, fall back to the last 20% and judge the candidate against
    # an absolute skill floor instead (production's score on data it has
    # already seen would be an unfairly leaked benchmark).
    meta = await db.ml_ensembles.find_one({"user_id": user_id}) or {}
    prod_trained_at = str(meta.get("trained_at") or "")
    post_prod = [t for t in trades
                 if str(t.get("closed_at") or "") > prod_trained_at] \
        if prod_trained_at else []
    if len(post_prod) >= MIN_HOLDOUT:
        hold_trades = post_prod
        train_trades = trades[:len(trades) - len(post_prod)]
        compare_mode = "vs_production"
    else:
        n_hold = max(MIN_HOLDOUT, int(len(trades) * HOLDOUT_FRAC))
        train_trades, hold_trades = trades[:-n_hold], trades[-n_hold:]
        compare_mode = "absolute_floor"
    if len(train_trades) < MIN_HOLDOUT:
        return {"stage": "replay", "status": "insufficient_data",
                "n_trades": len(trades)}
    X_train, y_train = await _build_xy(db, user_id, train_trades)
    X_hold, y_hold = await _build_xy(db, user_id, hold_trades)
    ev = await asyncio.to_thread(_holdout_auc_sync, X_train, y_train,
                                 X_hold, y_hold, user_id)
    if ev.get("status") != "ok" or ev.get("candidate_auc") is None:
        return {"stage": "shadow", "status": "skipped",
                "detail": ev.get("status", "no candidate score"),
                "n_trades": len(trades)}
    cand, prod = ev["candidate_auc"], ev.get("production_auc")
    if compare_mode == "vs_production" and prod is not None:
        floor = max(0.5, prod - AUC_TOLERANCE)
        basis = f"production {prod:.3f} on the same unseen window"
    else:
        floor = 0.55
        basis = "absolute skill floor (production benchmark would be leaked)"
    if cand < floor:
        return {"stage": "validation", "status": "rejected",
                "candidate_auc": round(cand, 3),
                "production_auc": round(prod, 3) if prod else None,
                "holdout_n": ev["holdout_n"], "compare_mode": compare_mode,
                "detail": f"candidate AUC {cand:.3f} < required {floor:.3f} "
                          f"({basis}) — production model untouched"}
    result = await train_ensemble(db, user_id)  # full-data refit → CANDIDATE only
    # round 12 P1-01 — the pipeline never reports "promoted": activation is a
    # separate two-admin, signed-manifest, atomic step (ml_ensemble.promote_candidate).
    return {"stage": "approval", "status": "candidate_ready_for_review",
            "candidate_auc": round(cand, 3),
            "production_auc": round(prod, 3) if prod else None,
            "holdout_n": ev["holdout_n"], "compare_mode": compare_mode,
            "approved_by": None,
            "promotion": "awaiting_two_admin_signed_manifest",
            "candidate_digest": (result.get("candidate") or {}).get("digest"),
            "production_digest": (result.get("production") or {}).get("digest"),
            "train_status": result.get("last_training_status")}


# ------------------------------------------------- versioned advisory nets
async def _snapshot_version(db, user_id: str, model: str, coll: str):
    doc = await (getattr(db, coll)).find_one({"user_id": user_id})
    if not doc:
        return
    doc.pop("_id", None)
    await db.model_versions.insert_one(
        {"user_id": user_id, "model": model, "doc": doc, "at": _now()})
    stale = db.model_versions.find(
        {"user_id": user_id, "model": model}).sort("at", -1).skip(VERSION_KEEP)
    async for old in stale:
        await db.model_versions.delete_one({"_id": old["_id"]})


# ---------------------------------------------------------- orchestration
async def gated_retrain(db, user_id: str, trigger: str = "") -> dict:
    """Full staged pass; writes an auditable `learning_runs` doc."""
    run = {"user_id": user_id, "trigger": trigger, "at": _now(), "stages": {}}
    frz = await freeze_check(db, user_id)
    run["frozen"] = frz["frozen"]
    run["freeze_reason"] = frz["reason"]
    if frz["frozen"]:
        run["stages"]["freeze"] = frz["reason"]
        await db.learning_runs.insert_one(dict(run))
        logger.info("learning FROZEN user=%s: %s", user_id, frz["reason"])
        return run
    try:
        run["stages"]["ml_ensemble"] = await staged_ml_retrain(db, user_id)
    except Exception as e:  # noqa: BLE001
        run["stages"]["ml_ensemble"] = {"status": f"failed: {e}"[:200]}
    for model, coll, trainer in (
            ("rl_policy", "rl_policies", "rl_policy"),
            ("bayes", "bayes_models", "bayes_decision")):
        try:
            await _snapshot_version(db, user_id, model, coll)
            mod = __import__(trainer)
            await (mod.train_policy(db, user_id) if model == "rl_policy"
                   else mod.train_model(db, user_id))
            run["stages"][model] = {"status": "versioned_update",
                                    "detail": "snapshot kept for rollback"}
        except Exception as e:  # noqa: BLE001
            run["stages"][model] = {"status": f"failed: {e}"[:200]}
    await db.learning_runs.insert_one(dict(run))
    run.pop("_id", None)
    return run


async def pipeline_status(db, user_id: str) -> dict:
    """For the UI: freeze state, last runs, shadow-lab queue."""
    frz = await freeze_check(db, user_id)
    last = await db.learning_runs.find(
        {"user_id": user_id}).sort("at", -1).to_list(length=5)
    for r in last:
        r.pop("_id", None)
        r["at"] = r["at"].isoformat() if hasattr(r["at"], "isoformat") else r["at"]
    shadow_counts = {}
    async for m in db.shadow_models.find({"user_id": user_id}, {"status": 1}):
        s = m.get("status") or "unknown"
        shadow_counts[s] = shadow_counts.get(s, 0) + 1
    versions = await db.model_versions.count_documents({"user_id": user_id})
    from ml_ensemble import public_state
    ml_state = public_state(await db.ml_ensembles.find_one({"user_id": user_id}) or {})
    return {"freeze": frz, "recent_runs": last,
            "shadow_lab": shadow_counts, "rollback_versions": versions,
            "ml_state": {"production": ml_state["production"], "candidate": ml_state["candidate"],
                         "promotion": ml_state["promotion"], "status": ml_state["status"]},
            "workflow": ["live_trades", "replay", "shadow", "validation",
                         "approval", "production"]}
