"""iter-159 — nightly automated resilience suite.

Every night (>=22h since the previous run, checked every 30 min so restarts
never skip a night) the loop runs:
  * chaos drills (12, incl. DR/rollback)
  * runtime validation (9 production-gate proofs)
  * a SEVERE stress test
Combined result → db.scheduled_drill_runs; any failure raises a CRITICAL
ops alert (deduped per day).
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("scheduled-drills")

CHECK_INTERVAL_SEC = int(os.environ.get("DRILL_CHECK_INTERVAL_SEC", "1800"))
MIN_GAP_HOURS = 22


def _now():
    return datetime.now(timezone.utc)


async def run_nightly_suite(db, actor: str = "scheduler") -> dict:
    from chaos_drills import run_drills
    from runtime_validation import run_runtime_validation
    from stress_test import run_stress_test

    chaos = await run_drills(db)
    chaos_failed = [r["drill"] for r in chaos["results"] if not r["passed"]]

    runtime = await run_runtime_validation(db, actor=actor)
    runtime_failed = [r["scenario"] for r in runtime["results"]
                      if r["status"] != "pass"]

    stress = await run_stress_test(db, severity="severe", actor=actor)
    stress_failed = [c["layer"] for c in stress["checks"]
                     if c["status"] != "pass"]

    failures = ([f"chaos:{n}" for n in chaos_failed]
                + [f"runtime:{n}" for n in runtime_failed]
                + [f"stress:{n}" for n in stress_failed])
    doc = {
        "run_id": f"nightly-{uuid.uuid4().hex[:10]}",
        "started_by": actor,
        "chaos": {"total": chaos.get("total"),
                  "passed": chaos.get("passed"),
                  "failed": len(chaos_failed)},
        "runtime": {"total": len(runtime["results"]),
                    "passed": runtime["passed"],
                    "failed": runtime["failed"],
                    "run_id": runtime["run_id"]},
        "stress": {"severity": "severe", "verdict": stress["verdict"],
                   "passed": stress["passed"], "failed": stress["failed"],
                   "run_id": stress["run_id"]},
        "failures": failures[:40],
        "ok": not failures,
        "at": _now(),
    }
    await db.scheduled_drill_runs.insert_one(dict(doc))
    if failures:
        from alerting import raise_alert
        await raise_alert(
            db, kind="scheduled_drills_failed", severity="critical",
            message=(f"nightly resilience suite FAILED "
                     f"({len(failures)}): {', '.join(failures[:8])}"),
            dedup_key=f"scheduled_drills:{_now().strftime('%Y-%m-%d')}")
        logger.error("nightly drills FAILED: %s", failures)
    else:
        logger.info("nightly drills all green (%s)", doc["run_id"])
    doc.pop("_id", None)
    return doc


async def _due(db) -> bool:
    last = await db.scheduled_drill_runs.find_one({}, sort=[("at", -1)])
    if not last:
        return True
    at = last["at"]
    if isinstance(at, str):
        at = datetime.fromisoformat(at.replace("Z", "+00:00"))
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return _now() - at >= timedelta(hours=MIN_GAP_HOURS)


async def scheduled_drill_loop():
    from database import get_db
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SEC)
            db = get_db()
            try:
                from release_channels import maybe_promote
                promoted = await maybe_promote(db)
                if promoted:
                    logger.info("canary candidate auto-promoted to stable")
            except Exception as e:  # noqa: BLE001
                logger.warning("release promotion check error: %s", e)
            try:
                from deployment_health import watch_deployment
                w = await watch_deployment(db)
                if w and w.get("status") == "auto_rollback":
                    logger.error("deployment auto-rollback executed: %s",
                                 w.get("reason"))
            except Exception as e:  # noqa: BLE001
                logger.warning("deployment health watch error: %s", e)
            try:  # iter-171 (#8) — periodically anchor the audit chain head
                from audit_anchor import create_anchor
                await create_anchor(db)
            except Exception as e:  # noqa: BLE001
                logger.warning("audit anchor error: %s", e)
            if await _due(db):
                try:
                    from correlation import new_correlation_id
                    new_correlation_id(prefix="wrk-nightly-drills-")
                except Exception:  # noqa: BLE001
                    pass
                await run_nightly_suite(db)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("scheduled drill loop error: %s", e)
