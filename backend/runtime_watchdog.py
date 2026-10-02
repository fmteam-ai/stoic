"""iter-183 — Runtime watchdog & crash forensics.

Production symptom: pod dies minutes after boot behind Cloudflare 520s with
no accessible logs. This module makes every crash self-documenting:

- an asyncio task heartbeats + samples RSS, persisting to Mongo every ~30s;
- an OS sentinel THREAD detects a blocked event loop (heartbeat stale) and
  captures the main thread's stack WHILE it is blocked;
- on boot, the previous run's final heartbeat is archived to
  db.runtime_crash_log — so after any crash the admin can read when it died,
  the memory at death, and the last captured blockage stack via
  GET /api/ops/runtime-stats (Admin → Ops Console card).
"""
import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger("watchdog")

SAMPLE_INTERVAL_SEC = int(os.environ.get("WATCHDOG_SAMPLE_SEC", "15"))
BLOCK_THRESHOLD_SEC = float(os.environ.get("WATCHDOG_BLOCK_SEC", "5"))

_boot_at = datetime.now(timezone.utc)
_heartbeat = time.monotonic()
_samples = deque(maxlen=60)
_last_blockage = None
_max_rss = 0.0
_thread_started = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def _check_blockage() -> dict | None:
    """Sentinel-thread check: if the event loop heartbeat is stale, capture
    the main thread's current stack (the code that is blocking it)."""
    global _last_blockage
    lag = time.monotonic() - _heartbeat
    if lag <= BLOCK_THRESHOLD_SEC:
        return None
    frames = sys._current_frames().get(threading.main_thread().ident)
    stack = "".join(traceback.format_stack(frames)) if frames else "(no frames)"
    _last_blockage = {
        "at": _now_iso(),
        "blocked_for_s": round(lag, 1),
        "rss_mb": round(rss_mb(), 1),
        "stack": stack[-4000:],
    }
    logger.critical("EVENT LOOP BLOCKED %.1fs (rss=%.0fMB) — main stack:\n%s",
                    lag, _last_blockage["rss_mb"], _last_blockage["stack"])
    return _last_blockage


def _sentinel_thread():
    while True:
        time.sleep(2)
        try:
            if _check_blockage():
                time.sleep(8)   # don't spam while still blocked
        except Exception:       # noqa: BLE001 — sentinel must never die
            pass


async def _record_previous_run(db) -> dict | None:
    """Archive the previous process's final heartbeat as a restart record."""
    prev = await db.runtime_health.find_one({"_id": "current"})
    if not prev or not prev.get("last_seen"):
        return None
    entry = {"boot_at": prev.get("boot_at"), "last_seen": prev["last_seen"],
             "pid": prev.get("pid"),
             "ended_rss_mb": prev.get("rss_mb"),
             "max_rss_mb": prev.get("max_rss_mb"),
             "last_blockage": prev.get("last_blockage"),
             "detected_at": _now_iso()}
    await db.runtime_crash_log.insert_one(dict(entry))
    stale = [d["_id"] async for d in db.runtime_crash_log
             .find({}, {"_id": 1}).sort("detected_at", -1).skip(25)]
    if stale:
        await db.runtime_crash_log.delete_many({"_id": {"$in": stale}})
    logger.critical(
        "PREVIOUS RUN ENDED — pid=%s last_seen=%s ended_rss=%sMB max_rss=%sMB "
        "blockage=%s", entry["pid"], entry["last_seen"], entry["ended_rss_mb"],
        entry["max_rss_mb"],
        (entry["last_blockage"] or {}).get("blocked_for_s"))
    return entry


async def _watchdog_task():
    global _heartbeat, _max_rss
    from database import get_db
    db = get_db()
    try:
        await _record_previous_run(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("crash-log archive failed: %s", e)
    tick = 0
    while True:
        _heartbeat = time.monotonic()
        r = rss_mb()
        _max_rss = max(_max_rss, r)
        _samples.append({"at": _now_iso(), "rss_mb": round(r, 1)})
        if tick % 2 == 0:
            try:
                await db.runtime_health.update_one(
                    {"_id": "current"},
                    {"$set": {"pid": os.getpid(),
                              "boot_at": _boot_at.isoformat(),
                              "last_seen": _now_iso(),
                              "rss_mb": round(r, 1),
                              "max_rss_mb": round(_max_rss, 1),
                              "last_blockage": _last_blockage}},
                    upsert=True)
            except Exception:  # noqa: BLE001
                pass
        if tick % 8 == 0:
            logger.info("watchdog: rss=%.0fMB max=%.0fMB uptime=%.0fmin",
                        r, _max_rss,
                        (datetime.now(timezone.utc) - _boot_at).total_seconds() / 60)
        tick += 1
        await asyncio.sleep(SAMPLE_INTERVAL_SEC)


def start_watchdog():
    global _thread_started
    faulthandler.enable()
    if not _thread_started:
        threading.Thread(target=_sentinel_thread, daemon=True,
                         name="loop-sentinel").start()
        _thread_started = True
    return asyncio.get_event_loop().create_task(_watchdog_task())


async def full_stats(db) -> dict:
    restarts = [d async for d in db.runtime_crash_log
                .find({}, {"_id": 0}).sort("detected_at", -1).limit(10)]
    up = (datetime.now(timezone.utc) - _boot_at).total_seconds()
    return {"pid": os.getpid(), "boot_at": _boot_at.isoformat(),
            "uptime_s": round(up),
            "rss_mb": round(rss_mb(), 1), "max_rss_mb": round(_max_rss, 1),
            "loop_lag_s": round(max(0.0, time.monotonic() - _heartbeat), 1),
            "last_blockage": _last_blockage,
            "samples": list(_samples)[-20:],
            "restarts": restarts}
