"""In-process hooks → security_events rows (processed by the agent within one tick)."""
import contextvars
import logging
from datetime import datetime, timedelta, timezone

from security_agent.redact import mask_obj

log = logging.getLogger("security_agent.events")
request_ip: contextvars.ContextVar = contextvars.ContextVar("security_request_ip", default=None)
KINDS = ("refresh_token_reuse", "step_up_missing", "order_auth_invalid", "bridge_invalid_token", "log_secret_hit")


async def emit(db, kind: str, detail: dict | None = None, *, ttl_days: int = 7) -> None:
    """Append-only, TTL 7 days; never raises into the caller's request."""
    try:
        now = datetime.now(timezone.utc)
        await db.security_events.insert_one({
            "kind": kind, "at": now.isoformat(), "ip": request_ip.get(),
            "detail": mask_obj(detail or {}), "processed": False,
            "expires_at": now + timedelta(days=ttl_days)})
    except Exception as e:  # noqa: BLE001
        log.warning("security event %s not recorded: %s", kind, e)


_THROTTLE: dict = {}


async def emit_throttled(db, kind: str, detail: dict | None = None, *, window_s: int = 10) -> None:
    """S15 — collapse a burst of identical (kind, ip) events into one row per window carrying a count."""
    import time as _t
    key = (kind, request_ip.get(), bool((detail or {}).get("retired")))
    now = _t.monotonic()
    ent = _THROTTLE.get(key)
    if ent and now - ent["t"] < window_s:
        ent["n"] += 1
        return
    n = (ent or {}).get("n", 0) + 1
    _THROTTLE[key] = {"t": now, "n": 0}
    if len(_THROTTLE) > 5000:
        _THROTTLE.clear()
    await emit(db, kind, {**(detail or {}), "count": n})


async def flush_denied_counts(db, state: dict, *, force: bool = False) -> int:
    """S12 — persist the API's per-IP 401/403/429 counters once per UTC minute (TTL 2 h);
    S13 — the swallowed-exception counters ride along for check P5."""
    minute = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    if state.get("minute") is None:
        state["minute"] = minute
    if state["minute"] == minute and not force:
        return 0
    by_ip, prev = dict(state.get("by_ip") or {}), state["minute"]
    state.update(minute=minute, by_ip={})
    n = 0
    try:
        exp = datetime.now(timezone.utc) + timedelta(hours=2)
        for ip, cnt in by_ip.items():
            await db.security_denied_counts.update_one({"minute": prev, "ip": ip}, {"$inc": {"n": int(cnt)}, "$set": {"expires_at": exp}}, upsert=True)
            n += 1
        try:
            from silent_failures import swallow_counters
            counters = dict(swallow_counters() or {})
        except Exception:  # noqa: BLE001
            counters = {}
        if counters:
            await db.security_signals.insert_one({"kind": "swallow_counters", "at": datetime.now(timezone.utc).isoformat(),
                                                  "counters": {str(k)[:80]: int(v) for k, v in counters.items()}, "expires_at": exp})
    except Exception as e:  # noqa: BLE001
        log.warning("denied-count flush failed: %s", type(e).__name__)
    return n
