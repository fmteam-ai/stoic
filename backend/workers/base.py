"""Shared worker runtime: leader lease + graceful loop supervision."""
import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

from secrets_loader import resolve_file_secrets  # noqa: E402
resolve_file_secrets()

from database import get_db  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("worker")

LEASE_TTL_SEC = 45
LEASE_RENEW_SEC = 15
HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def _try_acquire(db, name: str) -> bool:
    now = datetime.now(timezone.utc)
    res = await db.worker_leases.update_one(
        {"_id": name,
         "$or": [{"holder": HOLDER},
                 {"expires_at": {"$lt": now.isoformat()}},
                 {"expires_at": {"$exists": False}}]},
        {"$set": {"holder": HOLDER,
                  "expires_at": (now + timedelta(seconds=LEASE_TTL_SEC)).isoformat(),
                  "renewed_at": now.isoformat()}},
        upsert=False)
    if res.matched_count == 1:
        return True
    try:
        await db.worker_leases.insert_one(
            {"_id": name, "holder": HOLDER,
             "expires_at": (now + timedelta(seconds=LEASE_TTL_SEC)).isoformat(),
             "renewed_at": now.isoformat()})
        return True
    except Exception:
        return False


async def _lease_keeper(name: str, lost: asyncio.Event, loop_tasks=(),
                        loop_stats=None):
    db = get_db()
    total = len(loop_tasks)
    while True:
        await asyncio.sleep(LEASE_RENEW_SEC)
        try:
            ok = await _try_acquire(db, name)
            if not ok:
                logger.error("worker %s lost its lease — stopping loops", name)
                lost.set()
                return
            # loop-execution monitoring: leases prove the PROCESS is alive,
            # loops_running + per-loop supervisor stats prove every loop is
            # executing and making progress (not crash-looping).
            running = sum(1 for t in loop_tasks if not t.done())
            await db.worker_leases.update_one(
                {"_id": name, "holder": HOLDER},
                {"$set": {"loops_running": running, "loops_total": total,
                          "loops": loop_stats or {}}})
            if running < total:
                logger.error("worker %s: %d/%d loops running — a loop died",
                             name, running, total)
        except Exception as e:
            logger.warning("lease renew error for %s: %s", name, e)


def _supervise(name: str, loop_name: str, factory, stats: dict):
    """Restart a crashed loop with backoff and record telemetry: last error,
    consecutive failures (reset after 5 min of healthy running), restart
    count, last start time."""
    async def run():
        while True:
            started = asyncio.get_event_loop().time()
            stats[loop_name]["last_started_at"] = (
                datetime.now(timezone.utc).isoformat())
            try:
                await factory()
                raise RuntimeError("loop coroutine returned unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                st = stats[loop_name]
                ran_for = asyncio.get_event_loop().time() - started
                if ran_for > 300:          # made real progress before dying
                    st["consecutive_failures"] = 0
                st["consecutive_failures"] += 1
                st["restart_count"] += 1
                st["last_error"] = f"{type(e).__name__}: {e}"[:300]
                st["last_error_at"] = datetime.now(timezone.utc).isoformat()
                logger.exception("worker %s loop %s crashed (failure #%d) — "
                                 "restarting", name, loop_name,
                                 st["consecutive_failures"])
                await asyncio.sleep(min(60, 5 * st["consecutive_failures"]))
    return run


async def run_worker(name: str, loop_factories: list) -> None:
    """Acquire the leader lease for `name`, then run all loops until the
    lease is lost or the process is terminated. loop_factories: list of
    zero-arg callables returning coroutines."""
    db = get_db()
    logger.info("worker %s starting (holder=%s)", name, HOLDER)
    while not await _try_acquire(db, name):
        logger.info("worker %s standing by — another holder owns the lease",
                    name)
        await asyncio.sleep(LEASE_RENEW_SEC)
    logger.info("worker %s acquired leader lease", name)
    lost = asyncio.Event()
    loop_stats = {}
    tasks = []
    for i, f in enumerate(loop_factories):
        loop_name = getattr(f, "__name__", f"loop{i}") or f"loop{i}"
        if loop_name in loop_stats:
            loop_name = f"{loop_name}_{i}"
        loop_stats[loop_name] = {"consecutive_failures": 0, "restart_count": 0,
                                 "last_error": None, "last_error_at": None,
                                 "last_started_at": None}
        tasks.append(asyncio.create_task(
            _supervise(name, loop_name, f, loop_stats)()))
    tasks.append(asyncio.create_task(
        _lease_keeper(name, lost, tasks[:], loop_stats)))
    lost_waiter = asyncio.create_task(lost.wait())
    try:
        done, _ = await asyncio.wait([*tasks, lost_waiter],
                                     return_when=asyncio.FIRST_COMPLETED)
        for d in done:
            if d is not lost_waiter and d.exception():
                logger.error("worker %s loop crashed: %s", name, d.exception())
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("worker %s stopped", name)


def main(name: str, loop_factories: list) -> None:
    try:
        asyncio.run(run_worker(name, loop_factories))
    except KeyboardInterrupt:
        pass
