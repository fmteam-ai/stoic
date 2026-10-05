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
