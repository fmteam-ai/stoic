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
import hashlib
import logging
import math
import os
import shutil
from datetime import datetime, timezone

from pip_utils import price_to_pips
from rl_policy import _session_of, _regime_of
from model_manifest import MODEL_DIR, PRODUCTION_NAME, CANDIDATE_NAME, FEATURE_SCHEMA_VERSION  # noqa: F401

logger = logging.getLogger(__name__)

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

# iter-173 — shared OOM guard (see ml_runtime.py). Re-exported here because
# learning_pipeline and tests import it from ml_ensemble.
from ml_runtime import _memory_budget_gb, ml_runtime_enabled  # noqa: E402,F401


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


def train_sync(X: list, y: list, uid: str, *, window: dict | None = None) -> dict:
    """Walk-forward CV per GBM → out-of-sample AUC → skill weights, then
    refit on everything and persist a CANDIDATE (never production).
    Runs in a worker thread."""
    import joblib
    import json
    import numpy as np
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import TimeSeriesSplit
    from model_manifest import (sha256_file, sidecar_path, feature_code_digest,
                                running_build_sha)

    X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=int)
    dataset_sha256 = hashlib.sha256(X.tobytes() + b"|" + y.tobytes()).hexdigest()
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
    # round 11 P1-03 / round 12 P1-01 — online training NEVER self-promotes: the new
    # binary is a CANDIDATE (separate file + provenance sidecar) until two admins
    # approve it and a signed manifest names its digest (promote_candidate).
    cand = path / CANDIDATE_NAME
    joblib.dump(final, cand)
    digest = sha256_file(cand)
    now = datetime.now(timezone.utc).isoformat()
    holdout = round(sum(aucs.values()) / len(aucs), 3) if aucs else None
    prov = {"sha256": digest, "bytes": cand.stat().st_size, "trained_at": now,
            "feature_schema": FEATURE_SCHEMA_VERSION, "feature_code_digest": feature_code_digest(),
            "training_window": window or {"from": now, "until": now},
            "dataset_sha256": dataset_sha256, "n_samples": int(len(y)),
            "metrics": {"aucs": aucs, "weights": weights, "holdout_auc": holdout},
            "code_commit": running_build_sha()}
    sidecar_path(uid, "candidate").write_text(json.dumps(prov, indent=2, sort_keys=True))
    return {"aucs": aucs, "weights": weights, "models_saved": len(final),
            "candidate_digest": digest, "candidate_path": str(cand),
            "dataset_sha256": dataset_sha256, "training_window": prov["training_window"],
            "status": "candidate_ready_for_review"}


def _training_meta(user_id: str, n: int) -> dict:
    return {"user_id": user_id, "n_trades": n, "feature_names": FEATURE_NAMES,
            "last_training_at": datetime.now(timezone.utc).isoformat()}


async def _persist_training(db, user_id: str, meta: dict, candidate: dict | None) -> dict:
    """Candidate and production are SEPARATE records; training never touches production."""
    upd = {"$set": {**meta, **({"candidate": candidate} if candidate else {}),
                    "last_training_status": "candidate_ready_for_review" if candidate else meta.get("status")}}
    await db.ml_ensembles.update_one({"user_id": user_id}, upd, upsert=True)
    return await db.ml_ensembles.find_one({"user_id": user_id}) or {}


async def train_ensemble(db, user_id: str) -> dict:
    import asyncio
    from bson import ObjectId
    from datetime import timedelta
    if not ml_runtime_enabled():
        meta = {**_training_meta(user_id, 0), "status": "disabled_low_memory",
                "note": (f"container memory budget "
                         f"{_memory_budget_gb():.1f}GB is below the GBM-zoo "
                         "requirement — training skipped (set "
                         "ML_ENSEMBLE_ENABLED=true to force)")}
        return public_state(await _persist_training(db, user_id, meta, None))
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    trades = await db.trades.find({
        "user_id": user_id, "status": "closed", "pnl": {"$ne": None},
        "origin": "auto", "closed_at": {"$gte": since},
        "pnl_estimated": {"$ne": True}, "pnl_unknown": {"$ne": True},
    }).sort("closed_at", 1).to_list(5000)
    meta = _training_meta(user_id, len(trades))
    if len(trades) < MIN_TRADES:
        meta["status"] = "insufficient_data"
        return public_state(await _persist_training(db, user_id, meta, None))
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
    window = {"from": str(trades[0].get("closed_at")), "until": str(trades[-1].get("closed_at"))}
    result = await asyncio.to_thread(train_sync, X, y, user_id, window=window)
    candidate = {"status": "awaiting_approval", "digest": result["candidate_digest"],
                 "aucs": result["aucs"], "weights": result["weights"], "n_trades": len(trades),
                 "trained_at": meta["last_training_at"], "training_window": window,
                 "dataset_sha256": result["dataset_sha256"], "models_saved": result["models_saved"],
                 "feature_schema": FEATURE_SCHEMA_VERSION, "approvals": []}
    return public_state(await _persist_training(db, user_id, meta, candidate))


def public_state(doc: dict) -> dict:
    """ONE shape for /ml/ensemble, /ml/train, learning-pipeline and the UI:
    candidate and production are exposed separately, never merged."""
    doc = doc or {}
    prod, cand = doc.get("production"), doc.get("candidate")
    return {"status": ("production_active" if prod and prod.get("status") == "active" else "no_production_model"),
            "last_training_status": doc.get("last_training_status") or doc.get("status"),
            "last_training_at": doc.get("last_training_at") or doc.get("trained_at"),
            "n_trades": doc.get("n_trades"), "note": doc.get("note"),
            "production": ({k: prod.get(k) for k in ("status", "digest", "aucs", "weights", "n_trades", "trained_at",
                                                     "activated_at", "activated_by", "approvals", "manifest_sha256",
                                                     "training_window")} if prod else None),
            "candidate": ({k: cand.get(k) for k in ("status", "digest", "aucs", "weights", "n_trades", "trained_at",
                                                    "training_window", "approvals")} if cand else None),
            "promotion": ("candidate_ready_for_review" if cand and cand.get("status") == "awaiting_approval"
                          else "none_pending"),
            "min_trades_required": MIN_TRADES}


async def get_meta(db, user_id: str) -> dict:
    doc = await db.ml_ensembles.find_one({"user_id": user_id})
    if doc:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(doc.get("last_training_at") or doc["trained_at"])).total_seconds()
            if age < MODEL_TTL_HOURS * 3600:
                return doc
        except (KeyError, ValueError, TypeError):
            pass
    if not ml_runtime_enabled():
        # never auto-retrain in-request on a memory-constrained pod
        return doc or {"status": "disabled_low_memory"}
    await train_ensemble(db, user_id)
    return await db.ml_ensembles.find_one({"user_id": user_id}) or {}


async def approval_events(db, approvals: list) -> set:
    """Resolve approval audit-event ids against the hash-chained admin audit log."""
    ids = [str(a.get("audit_event_id") or "") for a in (approvals or [])]
    if not ids:
        return set()
    from model_manifest import APPROVAL_ACTION
    found = set()
    async for e in db.admin_audit_log.find({"entry_hash": {"$in": ids}, "action": APPROVAL_ACTION}, {"entry_hash": 1}):
        found.add(e["entry_hash"])
    return found


async def approve_candidate(db, user_id: str, approver_email: str, note: str = "") -> dict:
    """Two-person approval, step 1..n: an admin records an authenticated approval of
    the EXACT candidate digest. Returns the audit-event id used in the signed manifest."""
    from fastapi import HTTPException
    from audit_chain import append_chained
    doc = await db.ml_ensembles.find_one({"user_id": user_id}) or {}
    cand = doc.get("candidate")
    if not cand or cand.get("status") != "awaiting_approval":
        raise HTTPException(status_code=404, detail={"code": "no_candidate_awaiting_approval"})
    cand_file = MODEL_DIR / user_id / CANDIDATE_NAME
    from model_manifest import sha256_file, APPROVAL_ACTION
    if not cand_file.exists() or sha256_file(cand_file) != cand["digest"]:
        raise HTTPException(status_code=409, detail={"code": "candidate_bytes_changed"})
    if any(a.get("email") == approver_email.lower() for a in cand.get("approvals") or []):
        raise HTTPException(status_code=409, detail={"code": "repeated_approver"})
    ev = await append_chained(db, {"actor_email": approver_email.lower(), "action": APPROVAL_ACTION,
                                   "target_kind": "model_candidate", "target_id": user_id, "reason": (note or "")[:500],
                                   "at": datetime.now(timezone.utc).isoformat(),
                                   "meta": {"digest": cand["digest"], "user_id": user_id}})
    rec = {"email": approver_email.lower(), "audit_event_id": ev["entry_hash"], "at": ev["at"]}
    await db.ml_ensembles.update_one({"user_id": user_id, "candidate.digest": cand["digest"]},
                                     {"$push": {"candidate.approvals": rec}})
    return {"approval": rec, "approvals": (cand.get("approvals") or []) + [rec],
            "sign_hint": f"python -m model_manifest sign --promote {user_id} " + " ".join(
                f"--approval {a['email']}:{a['audit_event_id']}" for a in (cand.get("approvals") or []) + [rec])}


async def promote_candidate(db, user_id: str, actor_email: str) -> dict:
    """ATOMIC activation (round 12 P1-01): verify signed manifest → two distinct
    authenticated approvals → build binding → exact candidate bytes; then swap the
    binary and its matching metadata together, rolling back on any failure."""
    from fastapi import HTTPException
    from audit_chain import append_chained
    from model_manifest import (load_manifest, ModelRefused, sha256_file, manifest_digest,
                                check_build_binding, sidecar_path, _check_entry, feature_code_digest)
    doc = await db.ml_ensembles.find_one({"user_id": user_id}) or {}
    cand = doc.get("candidate")
    if not cand or cand.get("status") != "awaiting_approval":
        raise HTTPException(status_code=404, detail={"code": "no_candidate_awaiting_approval"})
    udir = MODEL_DIR / user_id
    cand_file, prod_file, prev_file = udir / CANDIDATE_NAME, udir / PRODUCTION_NAME, udir / "gbm_ensemble.previous.joblib"
    problems = []
    try:
        body = load_manifest()
    except ModelRefused as e:
        raise HTTPException(status_code=409, detail={"code": "manifest_refused", "problems": [str(e)]})
    entry = next((m for m in body["models"] if m["path"] == f"{user_id}/{PRODUCTION_NAME}"), None)
    if entry is None or entry["sha256"] != cand["digest"]:
        problems.append("signed manifest does not name the candidate digest for the production path")
    if not cand_file.exists() or sha256_file(cand_file) != cand["digest"]:
        problems.append("candidate bytes changed since training")
    if entry is not None:
        problems += [p for p in _check_entry(entry, cand_file, FEATURE_SCHEMA_VERSION, build=None,
                                             runtime_feature_digest=feature_code_digest())
                     if "never loadable" not in p]
    # promotion is a deliberate act on THIS code: bind to the running build (git HEAD in a bare checkout)
    from model_manifest import running_build_sha
    bb = check_build_binding(body, running_build_sha())
    if bb:
        problems.append(bb)
    known = await approval_events(db, body["approvals"])
    unknown = [a["email"] for a in body["approvals"] if a["audit_event_id"] not in known]
    if unknown:
        problems.append(f"approval audit event unknown for {', '.join(unknown)}")
    approved_digests = set()
    async for e in db.admin_audit_log.find({"entry_hash": {"$in": list(known)}}, {"meta": 1}):
        approved_digests.add((e.get("meta") or {}).get("digest"))
    if known and approved_digests != {cand["digest"]}:
        problems.append("approval events do not all approve this exact candidate digest")
    if problems:
        raise HTTPException(status_code=409, detail={"code": "promotion_refused", "problems": problems})

    had_prev = prod_file.exists()
    prev_sidecar = sidecar_path(user_id, "production")
    prev_sidecar_text = prev_sidecar.read_text() if prev_sidecar.exists() else None
    if had_prev:
        shutil.copy2(prod_file, prev_file)
    os.replace(cand_file, prod_file)
    cs = sidecar_path(user_id, "candidate")
    if cs.exists():
        os.replace(cs, prev_sidecar)
    now = datetime.now(timezone.utc).isoformat()
    production = {**{k: cand.get(k) for k in ("digest", "aucs", "weights", "n_trades", "trained_at", "training_window",
                                             "dataset_sha256", "feature_schema", "models_saved")},
                  "status": "active", "activated_at": now, "activated_by": actor_email,
                  "approvals": body["approvals"], "manifest_sha256": manifest_digest(),
                  "code_commit": body["code_commit"]}
    try:
        res = await db.ml_ensembles.update_one(
            {"user_id": user_id, "candidate.digest": cand["digest"]},
            {"$set": {"production": production, "candidate": None, "last_promotion_at": now}})
        if res.matched_count != 1:
            raise RuntimeError("candidate record changed during promotion")
    except Exception as e:  # noqa: BLE001 — roll the filesystem back, production unchanged
        if had_prev:
            os.replace(prev_file, prod_file)
        else:
            prod_file.unlink(missing_ok=True)
        if prev_sidecar_text is not None:
            prev_sidecar.write_text(prev_sidecar_text)
        _models_cache.pop(user_id, None)
        logger.error("model promotion rolled back for %s: %s", user_id, e)
        raise HTTPException(status_code=503, detail={"code": "promotion_store_unavailable"})
    prev_file.unlink(missing_ok=True)
    _models_cache.pop(user_id, None)
    await append_chained(db, {"actor_email": actor_email, "action": "model_promoted", "target_kind": "model",
                              "target_id": user_id, "reason": "two-admin signed manifest promotion", "at": now,
                              "meta": {"digest": cand["digest"], "manifest_sha256": production["manifest_sha256"],
                                       "approvals": [a["email"] for a in body["approvals"]],
                                       "code_commit": body["code_commit"]}})
    return public_state(await db.ml_ensembles.find_one({"user_id": user_id}) or {})


def _load_models(uid: str, known_events: set | None = None) -> dict:
    return _load_production(uid, known_events)[0]


def _load_production(uid: str, known_events: set | None = None) -> tuple:
    """(models, verified_digest) for the PRODUCTION binary only."""
    import joblib
    f = MODEL_DIR / uid / PRODUCTION_NAME
    if not f.exists():
        return {}, None
    mtime = f.stat().st_mtime
    hit = _models_cache.get(uid)
    if hit and hit[0] == mtime:
        return hit[1], hit[2]
    # round 11 P1-03 — joblib.load is code execution: only a binary named by the
    # SIGNED model manifest (exact digest + schema + approved) may be deserialized.
    from model_manifest import verify_model, ModelRefused
    try:
        digest = verify_model(f, known_events=known_events)
    except ModelRefused as e:
        logger.error("ensemble model REFUSED (quarantined, not loaded): %s", e)
        return {}, None
    try:
        models = joblib.load(f)
    except Exception as e:
        logger.warning("ensemble model load failed: %s", e)
        return {}, None
    _models_cache[uid] = (mtime, models, digest)
    return models, digest


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
    # OOM guard (iter-173): on memory-constrained pods skip ONLY the trained
    # GBM zoo (heavy imports); the light members (transformer / RL /
    # Bayesian) still vote — see ml_runtime_enabled().
    gbm_enabled = ml_runtime_enabled()
    meta = await get_meta(db, user_id) if gbm_enabled else {
        "status": "disabled_low_memory"}
    prod = (meta.get("production") or {}) if isinstance(meta, dict) else {}
    cand = (meta.get("candidate") or {}) if isinstance(meta, dict) else {}
    members = []
    used_digest = None
    # round 12 P1-01 — inference uses the PRODUCTION binary and PRODUCTION weights
    # only; the loaded digest must equal the production record's digest.
    if gbm_enabled and prod.get("status") == "active":
        known = await approval_events(db, prod.get("approvals") or [])
        models, digest = _load_production(user_id, known)
        if models and digest != prod.get("digest"):
            logger.error("production model digest %s != record %s — GBM vote withheld",
                         str(digest)[:12], str(prod.get("digest"))[:12])
            models = {}
        if models:
            used_digest = digest
            x = featurize(signal.get("action"), symbol, signal)
            scale = min((prod.get("n_trades") or 0) / 150.0, 1.0) * GBM_BLOCK_WEIGHT
            for name, mdl in models.items():
                w = float((prod.get("weights") or {}).get(name) or 0) * scale
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
            "gbm_status": ("production_active" if used_digest else
                           ("disabled_low_memory" if not gbm_enabled else
                            "no_production_model" if prod.get("status") != "active" else "production_refused")),
            "production_digest": used_digest,
            "candidate_status": cand.get("status"),
            "trained_n": prod.get("n_trades"),
            "gbm_auc": prod.get("aucs")}


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
