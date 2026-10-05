"""iter-109 · Online Learning — continuous model retraining.

Markets evolve; models must too. Instead of fixed TTLs, every learning
layer (ML ensemble GBMs, RL policy, Bayesian model) retrains when:
  · ≥ MIN_NEW_TRADES new closed trades since its last training run, OR
  · ≥ 1 new trade AND the models are older than MAX_STALENESS_S (1h).
Retraining with zero new trades is skipped — nothing changed to learn.
Sweep runs from the bot loop, self-throttled to once per 10 minutes."""
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

SWEEP_EVERY_S = 600
MIN_NEW_TRADES = 5
MAX_STALENESS_S = 3600

_last_sweep = 0.0


def should_retrain(new_trades: int, age_s: float) -> str | None:
    if new_trades >= MIN_NEW_TRADES:
        return f"{new_trades} new closed trades"
    if new_trades >= 1 and age_s > MAX_STALENESS_S:
        return f"{new_trades} new trade(s) and models {int(age_s // 60)}min old"
    return None


async def sweep_online_learning(db) -> int:
    global _last_sweep
    now = time.time()
    if now - _last_sweep < SWEEP_EVERY_S:
        return 0
    _last_sweep = now
    uids = await db.bot_configs.distinct("user_id", {"active": True})
    retrained = 0
    for uid in uids[:20]:
        try:
            n_closed = await db.trades.count_documents(
                {"user_id": uid, "status": "closed", "pnl": {"$ne": None},
                 "origin": "auto", "stats_excluded": {"$ne": True}})
            state = await db.online_learning.find_one({"user_id": uid}) or {}
            last_n = int(state.get("n_trades") or 0)
            age_s = MAX_STALENESS_S + 1
            try:
                age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(
                    state["last_trained"])).total_seconds()
            except (KeyError, ValueError):
                pass
            trigger = should_retrain(n_closed - last_n, age_s)
            if not state:  # first sight — snapshot the baseline, no retrain
                trigger = trigger or "initial baseline"
            if not trigger:
                continue
            # Phase 5 — staged pipeline: replay → shadow → validation →
            # approval → production, with a losing-streak freeze guard.
            # NEVER retrain-and-serve directly from live trades.
            from learning_pipeline import gated_retrain
            run = await gated_retrain(db, uid, trigger=trigger)
            results = {k: (v.get("status") if isinstance(v, dict) else v)
                       for k, v in run["stages"].items()}
            if run.get("frozen"):
                results["frozen"] = run["freeze_reason"]
            await db.online_learning.update_one(
                {"user_id": uid},
                {"$set": {"n_trades": n_closed,
                          "last_trained": datetime.now(timezone.utc).isoformat(),
                          "last_trigger": trigger, "last_results": results},
                 "$inc": {"retrain_count": 1}},
                upsert=True)
            retrained += 1
            logger.info("Online learning retrained user=%s (%s)", uid, trigger)
        except Exception as e:  # noqa: BLE001
            logger.exception("online learning sweep user=%s failed: %s", uid, e)
    return retrained
