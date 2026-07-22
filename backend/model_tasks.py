"""Phase F · Model service tasks — scheduled offline retraining + model
registry audit. DB-only: safe to run in an independent worker process.

The API process keeps serving its in-memory model (inline event-driven
retrains stay there); this worker guarantees a periodic full retrain is
persisted with an auditable registry trail even if the API is quiet.
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("model_worker")

MIN_RESOLVED = 200
MAX_KEYS_PER_RUN = 20


async def run_model_maintenance(db, min_resolved: int = MIN_RESOLVED) -> dict:
    """For every model key with enough resolved outcomes: full retrain
    (persisted artifact) + an audit row in scalp_model_audit."""
    from scalp import model as scalp_model
    keys = await db.scalp_decisions.distinct(
        "model_key", {"outcome.resolved": True})
    retrained, skipped = [], 0
    for key in (keys or [])[:MAX_KEYS_PER_RUN]:
        if not key or "|" not in str(key):
            skipped += 1
            continue
        broker, account_type, symbol = (str(key).split("|") + ["", ""])[:3]
        n = await db.scalp_decisions.count_documents(
            {"model_key": key, "outcome.resolved": True})
        if n < min_resolved:
            skipped += 1
            continue
        try:
            res = await scalp_model.retrain(db, symbol, broker=broker,
                                            account_type=account_type)
        except Exception as e:  # noqa: BLE001
            logger.exception("scheduled retrain failed key=%s: %s", key, e)
            continue
        await db.scalp_model_audit.insert_one({
            "model_key": key, "at": datetime.now(timezone.utc),
            "trigger": "scheduled", "resolved_n": n,
            "result": {k: res.get(k) for k in
                       ("status", "version", "auc", "brier", "n_train",
                        "deployed") if isinstance(res, dict)},
        })
        retrained.append(key)
    return {"retrained": retrained, "skipped": skipped,
            "keys_seen": len(keys or [])}
