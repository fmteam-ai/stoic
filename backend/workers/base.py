"""Shared worker runtime: leader lease + graceful loop supervision."""
import asyncio
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
from secrets_loader import resolve_file_secrets  # noqa: E402 — blanks → unset, *_FILE → value BEFORE the vault needs MONGO_URL
resolve_file_secrets()
try:   # sealed vault overlay (Admin → Integrations) — workers pick it up at start
    from integrations_settings import load_vault_sync as _load_vault
    _load_vault(os.environ["MONGO_URL"], os.environ["DB_NAME"])
except Exception:  # noqa: BLE001
    pass

from database import get_db  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
from security_agent.redact import install_log_filter as _install_redaction  # noqa: E402
_install_redaction()   # S5 — secrets masked in every worker process too
logger = logging.getLogger("worker")

LEASE_TTL_SEC = 45
LEASE_RENEW_SEC = 15
HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
# M117-6 — container health for workers (they serve no HTTP): the lease keeper / standby loop touches this file
# every LEASE_RENEW_SEC; the compose healthcheck fails when it is older than WORKER_HEARTBEAT_MAX_AGE_SEC.
HEARTBEAT_FILE = os.environ.get("WORKER_HEARTBEAT_FILE", "/tmp/stoic-worker-heartbeat")
HEARTBEAT_MAX_AGE_SEC = 90


def touch_heartbeat() -> None:
    try:
        with open(HEARTBEAT_FILE, "a"):
            os.utime(HEARTBEAT_FILE, None)
    except OSError as e:
        logger.warning("worker heartbeat file not writable (%s): %s", HEARTBEAT_FILE, e)


def heartbeat_healthy(path: str = HEARTBEAT_FILE, max_age: int = HEARTBEAT_MAX_AGE_SEC) -> bool:
    try:
        import time
        return (time.time() - os.path.getmtime(path)) < max_age
    except OSError:
        return False

# Per-loop progress telemetry — loops call record_progress() once per
# iteration; the lease keeper persists it (BSON datetimes) so readiness can
# distinguish "coroutine alive" from "coroutine making progress".
LOOP_PROGRESS: dict[str, dict] = {}


def record_progress(loop_name: str, processed: int = 0,
                    started_at: datetime | None = None,
                    interval_sec: int | None = None) -> None:
    now = datetime.now(timezone.utc)
    st = LOOP_PROGRESS.setdefault(loop_name, {"processed_count": 0})
    if started_at is not None:
        st["last_iteration_started_at"] = started_at
        st["last_duration_ms"] = int((now - started_at).total_seconds() * 1000)
    st["last_iteration_completed_at"] = now
    st["last_success_at"] = now
    if processed:
        st["last_progress_at"] = now
        st["processed_count"] = st.get("processed_count", 0) + int(processed)
    if interval_sec:
        st["expected_interval_sec"] = int(interval_sec)


async def persist_progress(db, loop_name: str) -> None:
    """Persist a loop's progress snapshot (safety review: readiness must see
    REAL per-iteration progress in BOTH deployment modes — the lease keeper
    covers dedicated workers; this covers in-process mode)."""
    st = LOOP_PROGRESS.get(loop_name)
    if not st:
        return
    await db.loop_progress.update_one(
        {"_id": loop_name}, {"$set": dict(st)}, upsert=True)


def _vault_key_id() -> str | None:
    try:
        from integrations_settings import master_key_id
        return master_key_id()
    except Exception:  # noqa: BLE001
        return None


async def _try_acquire(db, name: str) -> bool:
    now = datetime.now(timezone.utc)
    res = await db.worker_leases.update_one(
        {"_id": name,
         "$or": [{"holder": HOLDER},
                 {"expires_at": {"$lt": now}},
                 # legacy ISO-string leases are always stealable (a string
                 # never matches a $lt Date due to BSON type bracketing)
                 {"expires_at": {"$type": "string"}},
                 {"expires_at": {"$exists": False}}]},
        {"$set": {"holder": HOLDER,
                  "expires_at": now + timedelta(seconds=LEASE_TTL_SEC),
                  "renewed_at": now,
                  "vault_key_id": _vault_key_id()}},   # r29 P2-04: worker acknowledges the vault key it booted with
        upsert=False)
    if res.matched_count == 1:
        return True
    try:
        await db.worker_leases.insert_one(
            {"_id": name, "holder": HOLDER,
             "expires_at": now + timedelta(seconds=LEASE_TTL_SEC),
             "renewed_at": now})
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
            touch_heartbeat()
            # loop-execution monitoring: leases prove the PROCESS is alive,
            # loops_running + per-loop supervisor stats prove every loop is
            # executing, and record_progress() telemetry proves it is making
            # actual per-iteration progress (a blocked coroutine goes stale).
            running = sum(1 for t in loop_tasks if not t.done())
            merged = {k: dict(v) for k, v in (loop_stats or {}).items()}
            for k, v in LOOP_PROGRESS.items():
                merged.setdefault(k, {}).update(v)
            await db.worker_leases.update_one(
                {"_id": name, "holder": HOLDER},
                {"$set": {"loops_running": running, "loops_total": total,
                          "loops": merged}})
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
            stats[loop_name]["last_started_at"] = datetime.now(timezone.utc)
            try:
                from correlation import new_correlation_id
                new_correlation_id(prefix=f"wrk-{loop_name}-")
            except Exception:  # noqa: BLE001
                pass
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
                st["last_error_at"] = datetime.now(timezone.utc)
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
    os.environ.setdefault("STOIC_PROCESS_ROLE", f"worker-{name}")
    touch_heartbeat()
    while not await _try_acquire(db, name):
        logger.info("worker %s standing by — another holder owns the lease",
                    name)
        touch_heartbeat()   # standing by is healthy — the process is alive and polling
        await asyncio.sleep(LEASE_RENEW_SEC)
    logger.info("worker %s acquired leader lease", name)
    touch_heartbeat()
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
