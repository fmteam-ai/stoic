"""iter-109 · Online Learning — scheduled model retraining.

Markets evolve; models must too — but retraining (and re-testing the
candidate against a fixed floor) after every handful of trades is repeated
hypothesis testing: sooner or later a lucky candidate clears the bar.
So every learning layer (ML ensemble GBMs, RL policy, Bayesian model)
retrains only when BOTH hold:
  · ≥ MIN_NEW_TRADES (20, env ONLINE_MIN_NEW_TRADES) new closed trades
    since its last training run, AND
  · ≥ MIN_RETRAIN_INTERVAL_S (24h, env ONLINE_MIN_RETRAIN_HOURS) since the
    last training run.
Every run that reaches the ML validation stage counts as a PROMOTION TEST.
The number of tests in the trailing TEST_WINDOW_DAYS is passed to the
pipeline, which requires the candidate's block-bootstrap AUC lower bound at
a Bonferroni-adjusted level (α = 0.05 / n_tests) to clear the floor.
Sweep runs from the bot loop, self-throttled to once per 10 minutes."""
import logging
import os
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

SWEEP_EVERY_S = 600
MIN_NEW_TRADES = int(os.environ.get("ONLINE_MIN_NEW_TRADES", 20))
MIN_RETRAIN_INTERVAL_S = float(os.environ.get("ONLINE_MIN_RETRAIN_HOURS", 24)) * 3600
MAX_STALENESS_S = MIN_RETRAIN_INTERVAL_S   # legacy name (pre-validation)
TEST_WINDOW_DAYS = 30
MAX_TEST_LOG = 200

_last_sweep = 0.0


def should_retrain(new_trades: int, age_s: float, *,
                   min_new: int | None = None,
                   min_interval_s: float | None = None) -> str | None:
    """Reason string when a retrain is due, else None. Requires BOTH enough
    new evidence and enough time since the last run."""
    min_new = MIN_NEW_TRADES if min_new is None else int(min_new)
    min_interval_s = (MIN_RETRAIN_INTERVAL_S if min_interval_s is None
                      else float(min_interval_s))
    if new_trades >= min_new and age_s >= min_interval_s:
        return (f"{new_trades} new closed trades and models "
                f"{int(age_s // 3600)}h old")
    return None


def recent_tests(test_times: list, now: datetime | None = None,
                 days: int = TEST_WINDOW_DAYS) -> int:
    """Count promotion tests (ISO timestamps) inside the trailing window."""
    now = now or datetime.now(timezone.utc)
    lo = now - timedelta(days=days)
    n = 0
    for ts in test_times or []:
        try:
            d = datetime.fromisoformat(str(ts))
        except ValueError:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if d >= lo:
            n += 1
    return n


def _was_promotion_test(stage) -> bool:
    return isinstance(stage, dict) and (
        stage.get("promotion_test") or stage.get("stage") in ("validation",
                                                              "approval"))


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
                 "origin": "auto"})
            state = await db.online_learning.find_one({"user_id": uid}) or {}
            last_n = int(state.get("n_trades") or 0)
            age_s = MIN_RETRAIN_INTERVAL_S + 1
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
            test_log = list(state.get("promotion_test_times") or [])
            n_tests = recent_tests(test_log) + 1     # this run included
            run = await gated_retrain(db, uid, trigger=trigger,
                                      n_tests=n_tests)
            results = {k: (v.get("status") if isinstance(v, dict) else v)
                       for k, v in run["stages"].items()}
            if run.get("frozen"):
                results["frozen"] = run["freeze_reason"]
            upd = {"$set": {"n_trades": n_closed,
                            "last_trained": datetime.now(timezone.utc).isoformat(),
                            "last_trigger": trigger, "last_results": results},
                   "$inc": {"retrain_count": 1}}
            if _was_promotion_test(run["stages"].get("ml_ensemble")):
                test_log = (test_log + [datetime.now(timezone.utc).isoformat()]
                            )[-MAX_TEST_LOG:]
                upd["$set"]["promotion_test_times"] = test_log
                upd["$set"]["promotion_tests_window"] = recent_tests(test_log)
                upd["$inc"]["promotion_tests"] = 1
            await db.online_learning.update_one({"user_id": uid}, upd,
                                                upsert=True)
            retrained += 1
            logger.info("Online learning retrained user=%s (%s, test #%s)",
                        uid, trigger, n_tests)
        except Exception as e:  # noqa: BLE001
            logger.exception("online learning sweep user=%s failed: %s", uid, e)
    return retrained
