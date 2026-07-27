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
_fields: contextvars.ContextVar = contextvars.ContextVar(
    "log_fields", default=None)


def set_log_fields(**kw) -> None:
    """iter-161 — structured log context (trace/user/installation/deployment
    ids) attached to every record emitted in this task."""
    cur = dict(_fields.get() or {})
    for k, v in kw.items():
        if v is not None:
            cur[str(k)[:32]] = str(v)[:64]
    _fields.set(cur)


def reset_log_fields() -> None:
    _fields.set({})


def get_log_fields() -> dict:
    return dict(_fields.get() or {})


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
        f = _fields.get() or {}
        record.ctx = "".join(f" {k}={v}" for k, v in f.items())
        return True


def install(fmt: str | None = None) -> None:
    """Attach the filter to the root handlers and switch the format to one
    that includes the correlation id."""
    fmt = fmt or ("%(asctime)s - %(name)s - %(levelname)s - "
                  "[%(rid)s]%(ctx)s %(message)s")
    root = logging.getLogger()
    filt = CorrelationFilter()
    for h in root.handlers:
        h.addFilter(filt)
        h.setFormatter(logging.Formatter(fmt))
