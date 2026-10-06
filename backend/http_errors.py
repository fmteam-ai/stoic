"""SEC-002 — static error codes for HTTP responses.

The client receives {code, message, ref}: a stable machine-readable code, a generic
message and a short correlation ref. The ORIGINAL exception text never leaves the
server — it is logged once with the same ref so an operator can look it up.
"""
from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException

logger = logging.getLogger("stoic.http_errors")

GENERIC_MESSAGES = {
    400: "invalid request — see server log for the reference",
    401: "authentication failed",
    403: "forbidden",
    404: "not found",
    409: "conflict — the operation is not allowed in the current state",
    422: "invalid value",
    503: "service unavailable — see server log for the reference",
}


def error_detail(status: int, code: str, exc: BaseException | str, *, message: str | None = None) -> dict:
    ref = uuid.uuid4().hex[:10]
    logger.warning("[%s] %s (%d): %s", ref, code, status, exc)
    return {"code": code, "message": message or GENERIC_MESSAGES.get(status, "request failed"), "ref": ref}


def static_error(status: int, code: str, exc: BaseException | str, *, message: str | None = None) -> HTTPException:
    return HTTPException(status_code=status, detail=error_detail(status, code, exc, message=message))
