"""Structured accounting for intentionally-swallowed exceptions in
capital-critical paths (safety-review follow-up, iter-103).

Every suppressed failure produces:
  • a structured WARNING log line (JSON) — grep `swallowed_exception`
  • an in-process metric counter — GET /api/ops/swallowed
  • an operator-visible ops alert when material, or when one component
    suppresses ≥ ALERT_THRESHOLD failures within an hour.
The caller remains responsible for the safe fallback itself.
"""
import asyncio
import json
import logging
import time

logger = logging.getLogger("swallowed")

_counters: dict = {}   # (component, where) → total count
_windows: dict = {}    # component → [window_start_epoch, count]
ALERT_THRESHOLD = 20
WINDOW_SECS = 3600


def record_swallow(component: str, where: str, exc: Exception,
                   material: bool = False) -> None:
    key = (component, where)
    _counters[key] = _counters.get(key, 0) + 1
    logger.warning(json.dumps({
        "event": "swallowed_exception", "component": component,
        "where": where,
        "error": f"{type(exc).__name__}: {exc}"[:300]}))
    now = time.time()
    win = _windows.get(component)
    if not win or now - win[0] > WINDOW_SECS:
        win = [now, 0]
    win[1] += 1
    _windows[component] = win
    if material or win[1] == ALERT_THRESHOLD:
        _fire_alert(component, where, win[1], material)


def _fire_alert(component: str, where: str, count: int,
                material: bool) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def go():
        try:
            from alerting import raise_alert
            from database import get_db
            msg = (f"{component}.{where}: suppressed failure marked material"
                   if material else
                   f"{component}: {count} suppressed failures in the last "
                   f"hour — investigate")
            await raise_alert(get_db(), "swallowed_exceptions",
                              "critical" if material else "warning",
                              msg, dedup_key=f"swallow:{component}")
        except Exception as e:  # noqa: BLE001 — alerting must never recurse
            logger.warning("swallow alert failed: %s", e)

    loop.create_task(go())


def swallow_counters() -> dict:
    return {f"{c}.{w}": n for (c, w), n in sorted(_counters.items())}
