"""Stable public error responses — never leak raw exception text.

Usage:
    raise api_error(502, "market_data_unavailable",
                    "Market data is temporarily unavailable.", exc=e)
The full exception is logged internally with a request id that is also
returned to the client for support correlation.
"""
import logging
import uuid

from fastapi import HTTPException

logger = logging.getLogger("api.errors")


def api_error(status_code: int, code: str, message: str,
              exc: Exception | None = None) -> HTTPException:
    request_id = uuid.uuid4().hex[:12]
    if exc is not None:
        logger.error("[%s] %s: %s", request_id, code, exc, exc_info=exc)
    else:
        logger.warning("[%s] %s", request_id, code)
    return HTTPException(status_code=status_code, detail={
        "code": code, "message": message, "request_id": request_id})
