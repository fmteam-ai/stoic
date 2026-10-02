"""Explicit failure-semantics decorators for pre-trade guards.

Every pre-trade overlay is either CAPITAL-PROTECTING (an error must block
the trade) or ADVISORY (an error must not block — the hard gates below it
still apply). Historically that intent lived in ad-hoc ``try/except``
blocks and comments; these decorators make it visible and uniform.

    @fail_closed("risk_engine", block_reason="risk_engine_error")
    async def gate(...): ...      # exception → block dict, never raises

    @fail_open_advisory("execution_alpha", default=None)
    async def advise(...): ...    # exception → ``default``, never raises

Both log, bump an in-process counter (``guard_failure_counters()``) and
feed ``silent_failures.record_swallow`` so the Ops console / alerting see
them. ``asyncio.CancelledError`` (and other BaseExceptions) always
propagate — cancellation is not a guard verdict.

Block dict shape (``fail_closed``)::

    {"blocked": <block_reason>, "fail_closed": True, "guard": <name>,
     "error": "<exc text>", "error_type": "<ExcClass>",
     "reason": "<name> error — trade blocked (fail-closed): <exc>"}

``block_factory(name, exc) -> dict`` overrides the shape when a caller
needs its legacy contract (e.g. the Safety Guardian's ``{"ok": False}``);
with ``pass_args=True`` it is called as ``block_factory(name, exc, args,
kwargs)`` so the block can echo the guarded inputs.
"""
from __future__ import annotations

import functools
import inspect
import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("fail_closed")

_counters: dict = {}  # (mode, name) -> count


def _count(mode: str, name: str) -> None:
    _counters[(mode, name)] = _counters.get((mode, name), 0) + 1


def guard_failure_counters() -> dict:
    """{"closed:<name>": n, "advisory:<name>": n} — process-local metrics."""
    return {f"{m}:{n}": c for (m, n), c in sorted(_counters.items())}


def reset_counters() -> None:
    _counters.clear()


def _record(mode: str, name: str, exc: BaseException) -> None:
    _count(mode, name)
    try:
        from silent_failures import record_swallow
        record_swallow(f"guard_{mode}", name, exc)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — metrics must never mask the verdict
        pass


def default_block(name: str, block_reason: str, exc: BaseException) -> dict:
    return {"blocked": block_reason, "fail_closed": True, "guard": name,
            "error": str(exc)[:300], "error_type": type(exc).__name__,
            "reason": f"{name} error — trade blocked (fail-closed): {exc}"}


def block_on_error(name: str, exc: BaseException,
                   block_reason: Optional[str] = None,
                   log_level: int = logging.WARNING) -> dict:
    """Inline form of ``@fail_closed`` for guards that must stay inline
    (e.g. a loop body with ``continue``): logs, counts, records the swallow
    and returns the standard block dict. Use inside ``except Exception``."""
    _record("closed", name, exc)
    logger.log(log_level, "%s failed (fail-closed, blocking): %s: %s",
               name, type(exc).__name__, exc)
    return default_block(name, block_reason or f"{name}_error", exc)


def fail_closed(name: str, block_reason: Optional[str] = None, *,
                block_factory: Optional[Callable[[str, BaseException],
                                                 Any]] = None,
                pass_args: bool = False,
                log_level: int = logging.WARNING):
    """Capital-protecting guard: any ``Exception`` → a BLOCK verdict.

    Works on async and sync callables. The wrapped function's normal
    return value passes through untouched."""
    reason = block_reason or f"{name}_error"

    def _block(exc: Exception, a=(), k=None):
        _record("closed", name, exc)
        logger.log(log_level, "%s failed (fail-closed, blocking): %s: %s",
                   name, type(exc).__name__, exc)
        if block_factory is not None:
            if pass_args:
                return block_factory(name, exc, a, k or {})
            return block_factory(name, exc)
        return default_block(name, reason, exc)

    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def aw(*a, **k):
                try:
                    return await fn(*a, **k)
                except Exception as exc:  # noqa: BLE001
                    return _block(exc, a, k)
            aw.__guard_semantics__ = ("fail_closed", name)
            return aw

        @functools.wraps(fn)
        def sw(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as exc:  # noqa: BLE001
                return _block(exc, a, k)
        sw.__guard_semantics__ = ("fail_closed", name)
        return sw
    return deco


def fail_open_advisory(name: str, default: Any = None, *,
                       log_level: int = logging.WARNING):
    """Advisory annotation: any ``Exception`` → ``default`` (the caller
    proceeds as if the advisor had nothing to say). ONLY for overlays that
    can never be the last line of defence."""
    def _fallback(exc: Exception):
        _record("advisory", name, exc)
        logger.log(log_level, "%s failed (fail-open advisory): %s: %s",
                   name, type(exc).__name__, exc)
        return default() if callable(default) else default

    def deco(fn):
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def aw(*a, **k):
                try:
                    return await fn(*a, **k)
                except Exception as exc:  # noqa: BLE001
                    return _fallback(exc)
            aw.__guard_semantics__ = ("fail_open_advisory", name)
            return aw

        @functools.wraps(fn)
        def sw(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as exc:  # noqa: BLE001
                return _fallback(exc)
        sw.__guard_semantics__ = ("fail_open_advisory", name)
        return sw
    return deco
