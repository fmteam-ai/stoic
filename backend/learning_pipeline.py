"""Continuous-learning pipeline (Phase 5) — NO direct self-learning from
live trades. Every model update flows through the staged workflow:

    live trades → replay → shadow → validation → approval → production

  FREEZE GUARD   retraining is frozen after a short losing streak (≥5
                 consecutive losses inside 24h) or a tripped breaker —
                 the exact moment naive online learning would internalize
                 bad behavior.
  REPLAY         a CANDIDATE GBM ensemble is trained on the older 80% of
                 the trade history only (time-ordered split; training
                 trades whose holding window reaches into the holdout are
                 PURGED). Its deployable skill weights come from purged CV
                 on that training slice (ml_ensemble.cv_skill_weights).
  SHADOW         the candidate predicts the held-out most-recent 20% it
                 has never seen — a true out-of-sample shadow run. Holdout
                 must have ≥ MIN_HOLDOUT (25) trades.
  VALIDATION     candidate and production are scored with their DEPLOYED
                 weighting (skill weights, not an unweighted mean). The
                 candidate's block-bootstrap AUC LOWER bound — at a
                 Bonferroni-adjusted level for the number of promotion tests
                 already run (online_learning counts them) — must exceed the
                 floor: production AUC − 0.02 on the same unseen window, or
                 an absolute 0.55 when production's benchmark would be leaked.
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
import os
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("learning-pipeline")

STREAK_FREEZE_N = 5
FREEZE_HOURS = 24
HOLDOUT_FRAC = 0.2
MIN_HOLDOUT = 25           # min samples in the validation (holdout) fold
AUC_TOLERANCE = 0.02
ABS_AUC_FLOOR = 0.55
AUC_BOOT_ITERS = 1000
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
async def _build_xy(db, user_id: str, trades: list, *, with_times: bool = False):
    """Features/labels for `trades`; with_times=True also returns aligned
    (t_open, t_close) epoch-second lists for purged validation."""
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
    from ml_ensemble import trade_epoch
    X, y, t_open, t_close = [], [], [], []
    for t in trades:
        if not t.get("pnl"):
            continue
        sig = sigs.get(str(t.get("signal_id") or "")) or {}
        sig = {**sig,
               "entry_price": t.get("entry_price") or sig.get("entry_price"),
               # Entry-time SL only: trades.stop_loss is overwritten by every
               # trailing/breakeven modify, so winners would show a tiny
               # sl_pips / huge rr (target leakage). original_stop_loss is
               # written once at execution (execution.py).
               "stop_loss": (sig.get("stop_loss") or t.get("original_stop_loss")
                             or t.get("stop_loss"))}
        when = None
        try:
            when = datetime.fromisoformat(
                str(t.get("opened_at") or t.get("created_at")))
        except (ValueError, TypeError):
            pass
        X.append(featurize(t.get("action"), t.get("symbol"), sig, when))
        y.append(1 if float(t["pnl"]) > 0 else 0)
        t_open.append(trade_epoch(t.get("opened_at") or t.get("created_at")))
        t_close.append(trade_epoch(t.get("closed_at")))
    if with_times:
        return X, y, t_open, t_close
    return X, y


def _deployed_blend(models: dict, weights: dict | None, Xh):
    """P(win) exactly as the GBM block is deployed: Σ wᵢpᵢ / Σ wᵢ over the
    members with a positive learned weight. weights=None → legacy equal
    weighting (production records predating learned weights). Returns None
    when no member carries weight (the block would not vote live)."""
    import numpy as np
    preds, ws = [], []
    for name, mdl in models.items():
        w = 1.0 if weights is None else float((weights or {}).get(name) or 0)
        if w <= 0:
            continue
        try:
            preds.append(mdl.predict_proba(Xh)[:, 1])
            ws.append(w)
        except Exception:
            continue
    if not preds:
        return None
    return np.average(np.vstack(preds), axis=0, weights=np.asarray(ws))


def _holdout_auc_sync(X_train, y_train, X_hold, y_hold, uid: str, *,
                      prod_weights: dict | None = None,
                      t_open_train=None, t_close_train=None,
                      n_tests: int = 1) -> dict:
    """Train a candidate on the older split, score BOTH candidate and the
    current production ensemble on the never-seen holdout with their
    DEPLOYED weighting. Also returns the candidate's block-bootstrap AUC
    lower bound at a Bonferroni-adjusted level for `n_tests`. Thread-safe."""
    import numpy as np
    from sklearn.metrics import roc_auc_score
    from ml_ensemble import _gbm_zoo, _load_models, cv_skill_weights
    from validation import bootstrap_auc_lower_bound, multiple_testing_alpha

    Xt, yt = np.asarray(X_train, float), np.asarray(y_train, int)
    Xh, yh = np.asarray(X_hold, float), np.asarray(y_hold, int)
    if len(set(yt)) < 2 or len(set(yh)) < 2:
        return {"status": "degenerate_labels"}

    # the weights the candidate WOULD deploy with (purged CV on train only)
    cvres = cv_skill_weights(Xt, yt, t_open_train, t_close_train)
    candidate = {}
    for name, mdl in _gbm_zoo().items():
        try:
            mdl.fit(Xt, yt)
            candidate[name] = mdl
        except Exception:  # noqa: BLE001
            continue
    p_cand = _deployed_blend(candidate, cvres["weights"], Xh)
    p_prod = _deployed_blend(_load_models(uid) or {}, prod_weights, Xh)
    cand_auc = float(roc_auc_score(yh, p_cand)) if p_cand is not None else None
    prod_auc = float(roc_auc_score(yh, p_prod)) if p_prod is not None else None
    alpha = multiple_testing_alpha(n_tests)
    lb = (bootstrap_auc_lower_bound(yh, p_cand, alpha=alpha,
                                    iters=AUC_BOOT_ITERS)
          if p_cand is not None else None)
    return {"status": "ok", "candidate_auc": cand_auc,
            "production_auc": prod_auc, "holdout_n": int(len(yh)),
            "candidate_auc_lower": lb, "alpha": round(alpha, 4),
            "n_tests": int(n_tests), "candidate_weights": cvres["weights"],
            "candidate_cv": cvres["cv"]}


def purge_train(train_trades: list, hold_trades: list) -> list:
    """Drop training trades whose holding window reaches the holdout's first
    open (their outcome overlaps the validation period)."""
    from ml_ensemble import trade_epoch
    opens = [trade_epoch(t.get("opened_at") or t.get("created_at"))
             for t in hold_trades]
    opens = [o for o in opens if o is not None]
    if not opens:
        return train_trades
    first = min(opens)
    out = []
    for t in train_trades:
        c = trade_epoch(t.get("closed_at"))
        if c is not None and c >= first:
            continue
        out.append(t)
    return out


async def staged_ml_retrain(db, user_id: str, *, n_tests: int = 1) -> dict:
    """replay → shadow → validation → approval → production for the GBMs.
    `n_tests` = promotion tests run so far in the window INCLUDING this one
    (online_learning tracks it) — tightens the bootstrap bound."""
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
    prod_trained_at = str((meta.get("production") or {}).get("trained_at") or "")
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
    train_trades = purge_train(train_trades, hold_trades)
    if len(train_trades) < MIN_HOLDOUT or len(hold_trades) < MIN_HOLDOUT:
        return {"stage": "replay", "status": "insufficient_data",
                "n_trades": len(trades),
                "detail": f"need ≥{MIN_HOLDOUT} purged train and holdout trades"}
    X_train, y_train, to_tr, tc_tr = await _build_xy(
        db, user_id, train_trades, with_times=True)
    X_hold, y_hold = await _build_xy(db, user_id, hold_trades)
    # warm the production model from the shared artifact store so the thread can score it
    prod_rec = (meta.get("production") or {})
    if prod_rec.get("status") == "active":
        from ml_ensemble import _load_production, approval_events
        await _load_production(db, user_id, prod_rec, await approval_events(db, prod_rec.get("approvals") or []))
    ev = await asyncio.to_thread(
        _holdout_auc_sync, X_train, y_train, X_hold, y_hold, user_id,
        prod_weights=(prod_rec.get("weights") or None),
        t_open_train=to_tr, t_close_train=tc_tr, n_tests=max(1, int(n_tests)))
    if ev.get("status") != "ok" or ev.get("candidate_auc") is None:
        detail = ev.get("status", "no candidate score")
        if ev.get("status") == "ok":
            detail = ("candidate has no deployable GBM weight ("
                      f"{(ev.get('candidate_cv') or {}).get('status')})")
        return {"stage": "shadow", "status": "skipped", "detail": detail,
                "n_trades": len(trades)}
    cand, prod = ev["candidate_auc"], ev.get("production_auc")
    lb = ev.get("candidate_auc_lower")
    if compare_mode == "vs_production" and prod is not None:
        floor = max(0.5, prod - AUC_TOLERANCE)
        basis = f"production {prod:.3f} on the same unseen window"
    else:
        floor = ABS_AUC_FLOOR
        basis = "absolute skill floor (production benchmark would be leaked)"
    common = {"candidate_auc": round(cand, 3),
              "candidate_auc_lower": round(lb, 3) if lb is not None else None,
              "production_auc": round(prod, 3) if prod is not None else None,
              "holdout_n": ev["holdout_n"], "compare_mode": compare_mode,
              "alpha": ev.get("alpha"), "n_tests": ev.get("n_tests"),
              "weighting": "deployed_skill_weights", "promotion_test": True}
    if lb is None or lb <= floor:
        return {"stage": "validation", "status": "rejected", **common,
                "detail": f"candidate AUC {cand:.3f}, block-bootstrap lower "
                          f"bound {lb if lb is None else round(lb, 3)} "
                          f"(α={ev.get('alpha')} for test #{ev.get('n_tests')}) "
                          f"not above required {floor:.3f} ({basis}) — "
                          f"production model untouched"}
    result = await train_ensemble(db, user_id)  # full-data refit → CANDIDATE only
    # round 12 P1-01 — the pipeline never reports "promoted": activation is a
    # separate two-admin, signed-manifest, atomic step (ml_ensemble.promote_candidate).
    return {"stage": "approval", "status": "candidate_ready_for_review",
            **common,
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


def meta_labeling_enabled() -> bool:
    """META_LABELING_ENABLED (default true) — kill switch for the
    triple-barrier meta-label retrain stage."""
    return os.environ.get("META_LABELING_ENABLED", "true").strip().lower() \
        not in ("0", "false", "no", "off")


async def _meta_labeling_stage(db, user_id: str,
                               account_id: str | None = None) -> dict:
    """Retrain the triple-barrier meta-label model (meta_labeling.py) for
    this owner. ADVISORY: any failure is logged and recorded on the run —
    it must never block the rest of the pipeline or trading."""
    if not meta_labeling_enabled():
        return {"status": "disabled", "detail": "META_LABELING_ENABLED=false"}
    try:
        import meta_labeling
        res = await meta_labeling.retrain(db, user_id, account_id)
        res = res or {}
        return {"status": "trained" if res.get("trained") else "skipped",
                **{k: res.get(k) for k in ("n_events", "n", "n_pos", "reason",
                                           "trained_at") if k in res}}
    except Exception as e:  # noqa: BLE001 — advisory
        logger.warning("meta-labeling retrain failed user=%s acct=%s: %s",
                       user_id, account_id, e)
        return {"status": f"failed: {e}"[:200]}


# ---------------------------------------------------------- orchestration
async def gated_retrain(db, user_id: str, trigger: str = "", *,
                        n_tests: int = 1,
                        account_id: str | None = None) -> dict:
    """Full staged pass; writes an auditable `learning_runs` doc.
    `n_tests` is forwarded to the ML validation stage (multiple-testing).
    The triple-barrier meta-label model is retrained as an advisory stage
    (`meta_labeling`, switch META_LABELING_ENABLED)."""
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
        run["stages"]["ml_ensemble"] = await staged_ml_retrain(
            db, user_id, n_tests=n_tests)
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
    run["stages"]["meta_labeling"] = await _meta_labeling_stage(
        db, user_id, account_id)
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
