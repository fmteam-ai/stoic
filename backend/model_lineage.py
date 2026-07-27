"""iter-161 — AI model versioning, feature lineage and replay validation.

Every signal is stamped with the model version (content hash of the AI
decision modules) and its feature lineage (which decision inputs were
present + a stable hash). Replay validation re-derives both against the
current codebase to prove whether a historical decision is reproducible.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

MODEL_FILES = ("ai_signals.py", "bayes_decision.py", "calibration.py",
               "risk_engine.py", "explainer.py", "drift_detector.py",
               "monte_carlo.py", "consensus.py", "confluence.py",
               "entropy_filter.py")

LINEAGE_KEYS = frozenset({
    "symbol", "action", "confidence", "entry_price", "stop_loss",
    "take_profit", "tp1", "tp2", "tp3", "rr_ratio", "sl_pips",
    "market_regime", "session_feats", "mtf", "monte_carlo", "risk_engine",
    "meta_strategy", "adaptive_sizing", "strategy_class", "engine_label",
    "consensus", "bayes", "entropy", "confluence", "calibrated_p_win",
    "pipeline_safe_to_execute", "adaptive_auto_preset",
})

_cache: dict = {}


def model_version() -> dict:
    """Deterministic version id from the AI module contents (cached)."""
    if "v" in _cache:
        return _cache["v"]
    base = Path(__file__).parent
    files = {}
    h = hashlib.sha256()
    for name in MODEL_FILES:
        p = base / name
        if p.exists():
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            files[name] = digest[:12]
            h.update(f"{name}:{digest}".encode())
    _cache["v"] = {"version": f"m-{h.hexdigest()[:12]}", "files": files}
    return _cache["v"]


def feature_lineage(signal: dict) -> dict:
    present = sorted(k for k in signal
                     if k in LINEAGE_KEYS and signal.get(k) is not None)
    fh = hashlib.sha256(json.dumps(present).encode()).hexdigest()[:16]
    return {"features": present, "feature_set_hash": fh}


async def stamp_lineage(db, signal: dict) -> dict:
    """Attach {version, features, feature_set_hash} and register the model
    version on first sight."""
    mv = model_version()
    signal["model"] = {"version": mv["version"], **feature_lineage(signal)}
    if not _cache.get("registered"):
        await db.model_code_versions.update_one(
            {"_id": mv["version"]},
            {"$setOnInsert": {
                "files": mv["files"],
                "first_seen": datetime.now(timezone.utc).isoformat()}},
            upsert=True)
        _cache["registered"] = True
    return signal["model"]


async def replay_validate(db, signal_id: str) -> dict | None:
    """Re-derive lineage + explanation for a stored signal and compare
    against what was recorded at decision time."""
    from bson import ObjectId
    from bson.errors import InvalidId
    try:
        sig = await db.signals.find_one({"_id": ObjectId(str(signal_id))})
    except (InvalidId, TypeError):
        sig = None
    if not sig:
        return None
    stored = sig.get("model") or {}
    cur = model_version()
    lin = feature_lineage(sig)
    checks = {
        "model_version_match": stored.get("version") == cur["version"],
        "feature_set_match": (stored.get("feature_set_hash")
                              == lin["feature_set_hash"]),
    }
    explanation_reproducible = None
    if sig.get("explanation") is not None:
        try:
            from explainer import explain_decision
            explanation_reproducible = (explain_decision(dict(sig))
                                        == sig.get("explanation"))
        except Exception:  # noqa: BLE001
            explanation_reproducible = None
    if not stored:
        verdict = "no_lineage_recorded"
    elif checks["model_version_match"] and checks["feature_set_match"]:
        verdict = "reproducible"
    else:
        verdict = "drifted"
    return {"signal_id": str(sig["_id"]),
            "stored_model": stored or None,
            "current_model": {"version": cur["version"]},
            "feature_lineage_now": lin,
            "checks": {**checks,
                       "explanation_reproducible": explanation_reproducible},
            "verdict": verdict}
