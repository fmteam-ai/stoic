"""iter-158 — correlation IDs across API, workers and background tasks.

The request-id middleware (iter-152) already stamps X-Request-ID on every
response and access-log line. This module makes that id flow into EVERY log
record emitted while handling the request (and gives workers a per-loop id),
so one grep traces a problem end-to-end.
"""
import contextvars
import logging
import uuid

_current: contextvars.ContextVar = contextvars.ContextVar(
    "correlation_id", default="-")


def set_correlation_id(rid: str) -> None:
    _current.set((rid or "-")[:64])


def get_correlation_id() -> str:
    return _current.get()


def new_correlation_id(prefix: str = "") -> str:
    rid = f"{prefix}{uuid.uuid4().hex[:12]}"
    _current.set(rid)
    return rid


class CorrelationFilter(logging.Filter):
    """Injects .rid into every record so formatters can print it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.rid = _current.get()
        return True


def install(fmt: str | None = None) -> None:
    """Attach the filter to the root handlers and switch the format to one
    that includes the correlation id."""
    fmt = fmt or ("%(asctime)s - %(name)s - %(levelname)s - "
                  "[%(rid)s] %(message)s")
    root = logging.getLogger()
    filt = CorrelationFilter()
    for h in root.handlers:
        h.addFilter(filt)
        h.setFormatter(logging.Formatter(fmt))
