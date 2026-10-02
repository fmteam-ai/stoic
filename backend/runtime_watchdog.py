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
# Event-loop lag: sleep LAG_PROBE_SEC, measure how late we wake up.
LAG_PROBE_SEC = float(os.environ.get("WATCHDOG_LAG_PROBE_SEC", "1"))
LAG_WARN_MS = float(os.environ.get("WATCHDOG_LAG_WARN_MS", "500"))
LAG_LOG_EVERY_SEC = 30.0      # rate-limit the lag warning while degraded

_boot_at = datetime.now(timezone.utc)
_heartbeat = time.monotonic()
_samples = deque(maxlen=60)
_last_blockage = None
_max_rss = 0.0
_thread_started = False

# event-loop lag telemetry (ms) — last 5 min of 1 s probes
_lag_window = deque(maxlen=300)
_lag = {"last_ms": 0.0, "max_ms": 0.0, "over_threshold_total": 0,
        "samples_total": 0, "last_over_at": None}


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
                              "loop_lag": lag_stats(),
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


def record_lag(lag_ms: float) -> None:
    """Record one event-loop lag probe (ms)."""
    lag_ms = max(0.0, float(lag_ms))
    _lag_window.append(lag_ms)
    _lag["last_ms"] = round(lag_ms, 1)
    _lag["max_ms"] = round(max(_lag["max_ms"], lag_ms), 1)
    _lag["samples_total"] += 1
    if lag_ms > LAG_WARN_MS:
        _lag["over_threshold_total"] += 1
        _lag["last_over_at"] = _now_iso()


def lag_stats() -> dict:
    """Event-loop lag summary (ms): last / max since boot / p50 / p99 over
    the last ~5 min, and how many probes exceeded LAG_WARN_MS."""
    w = sorted(_lag_window)

    def pct(p):
        if not w:
            return 0.0
        return round(w[min(len(w) - 1, int(round(p * (len(w) - 1))))], 1)
    return {**_lag, "p50_ms": pct(0.50), "p99_ms": pct(0.99),
            "window_samples": len(w), "warn_threshold_ms": LAG_WARN_MS}


def prometheus_lines() -> list[str]:
    """Prometheus text-format lines for /api/metrics (per process)."""
    st = lag_stats()
    pid = os.getpid()
    out = []
    for name, key, help_txt in (
            ("stoic_event_loop_lag_ms", "last_ms", "Event-loop lag, last 1s probe (ms)"),
            ("stoic_event_loop_lag_p99_ms", "p99_ms", "Event-loop lag p99 over ~5 min (ms)"),
            ("stoic_event_loop_lag_max_ms", "max_ms", "Event-loop lag max since boot (ms)"),
            ("stoic_event_loop_lag_over_threshold_total", "over_threshold_total",
             f"Lag probes above {LAG_WARN_MS:.0f} ms since boot")):
        out += [f"# HELP {name} {help_txt}", f"# TYPE {name} gauge",
                f'{name}{{pid="{pid}"}} {st[key]}']
    return out


async def loop_lag_monitor(probe_sec: float | None = None, sleep=asyncio.sleep,
                           clock=time.monotonic):
    """Sleep `probe_sec` repeatedly; the oversleep is the event-loop lag
    (time other coroutines / blocking calls held the loop). Logs a warning
    (rate-limited) whenever lag exceeds LAG_WARN_MS."""
    probe = LAG_PROBE_SEC if probe_sec is None else probe_sec
    last_log = -LAG_LOG_EVERY_SEC
    while True:
        t0 = clock()
        await sleep(probe)
        lag_ms = (clock() - t0 - probe) * 1000.0
        record_lag(lag_ms)
        if lag_ms > LAG_WARN_MS:
            now = clock()
            if now - last_log >= LAG_LOG_EVERY_SEC:
                last_log = now
                logger.warning("event loop lag %.0fms (> %.0fms) — p99=%.0fms "
                               "over_threshold=%d", lag_ms, LAG_WARN_MS,
                               lag_stats()["p99_ms"], _lag["over_threshold_total"])


def start_watchdog(registry=None):
    """Start the sentinel thread + watchdog + event-loop lag monitor. With a
    workers.registry.TaskRegistry the tasks are supervised and cancelled on
    shutdown; without one they are plain tasks (legacy callers)."""
    global _thread_started
    faulthandler.enable()
    if not _thread_started:
        threading.Thread(target=_sentinel_thread, daemon=True,
                         name="loop-sentinel").start()
        _thread_started = True
    if registry is not None:
        registry.spawn("event_loop_lag", loop_lag_monitor)
        return registry.spawn("runtime_watchdog", _watchdog_task)
    loop = asyncio.get_event_loop()
    loop.create_task(loop_lag_monitor())
    return loop.create_task(_watchdog_task())


async def full_stats(db) -> dict:
    restarts = [d async for d in db.runtime_crash_log
                .find({}, {"_id": 0}).sort("detected_at", -1).limit(10)]
    up = (datetime.now(timezone.utc) - _boot_at).total_seconds()
    return {"pid": os.getpid(), "boot_at": _boot_at.isoformat(),
            "uptime_s": round(up),
            "rss_mb": round(rss_mb(), 1), "max_rss_mb": round(_max_rss, 1),
            "loop_lag_s": round(max(0.0, time.monotonic() - _heartbeat), 1),
            "event_loop_lag_ms": lag_stats(),
            "last_blockage": _last_blockage,
            "samples": list(_samples)[-20:],
            "restarts": restarts}
