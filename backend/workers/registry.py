"""Supervised background-task registry for the API process.

Replaces fire-and-forget ``asyncio.create_task`` calls at startup:

* every task is NAMED and tracked (no orphaned / garbage-collected tasks);
* a crashed (or unexpectedly returning) coroutine is restarted with
  exponential backoff — the same contract as ``workers.base._supervise``
  (consecutive-failure counter reset after 5 min of healthy running);
* ``shutdown()`` cancels everything and awaits it with a timeout, so no
  task is left running against a closed Mongo client.

Deliberately free of import-time side effects (unlike ``workers.base``,
which loads env/vault/secrets on import) so server.py can import it early.
"""
import asyncio
import logging
from datetime import datetime, timezone

logger = logging.getLogger("task-registry")

HEALTHY_RESET_SEC = 300


class TaskRegistry:
    def __init__(self, *, base_backoff: float = 1.0, max_backoff: float = 60.0):
        self._tasks: dict[str, asyncio.Task] = {}
        self._stats: dict[str, dict] = {}
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._closing = False

    # ── lifecycle ───────────────────────────────────────────────────────
    def spawn(self, name: str, factory, *, restart: bool = True) -> asyncio.Task:
        """Run ``factory()`` (zero-arg callable returning an awaitable) under
        supervision. restart=False runs it once (crash is logged, not retried)."""
        if name in self._tasks and not self._tasks[name].done():
            raise ValueError(f"task {name!r} already running")
        self._stats[name] = {"restart_count": 0, "consecutive_failures": 0,
                             "last_error": None, "last_error_at": None,
                             "last_started_at": None, "restart": restart}
        task = asyncio.get_running_loop().create_task(
            self._supervise(name, factory, restart), name=f"bg:{name}")
        self._tasks[name] = task
        return task

    def backoff_for(self, failures: int) -> float:
        return min(self.max_backoff, self.base_backoff * (2 ** max(0, failures - 1)))

    async def _supervise(self, name: str, factory, restart: bool):
        loop = asyncio.get_running_loop()
        st = self._stats[name]
        while True:
            started = loop.time()
            st["last_started_at"] = datetime.now(timezone.utc)
            try:
                await factory()
                if not restart:
                    return
                raise RuntimeError("background coroutine returned unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — supervisor must survive
                if loop.time() - started > HEALTHY_RESET_SEC:
                    st["consecutive_failures"] = 0
                st["consecutive_failures"] += 1
                st["last_error"] = f"{type(e).__name__}: {e}"[:300]
                st["last_error_at"] = datetime.now(timezone.utc)
                if not restart or self._closing:
                    logger.exception("background task %s failed (not restarted)", name)
                    return
                st["restart_count"] += 1
                delay = self.backoff_for(st["consecutive_failures"])
                logger.exception("background task %s crashed (failure #%d) — "
                                 "restarting in %.0fs", name,
                                 st["consecutive_failures"], delay)
                await asyncio.sleep(delay)

    async def shutdown(self, timeout: float = 10.0) -> list[str]:
        """Cancel every task and wait (bounded) for them to finish. Returns
        the names of tasks that did not stop within ``timeout``."""
        self._closing = True
        pending = {n: t for n, t in self._tasks.items() if not t.done()}
        for t in pending.values():
            t.cancel()
        if not pending:
            return []
        done, still = await asyncio.wait(pending.values(), timeout=timeout)
        stuck = [n for n, t in pending.items() if t in still]
        if stuck:
            logger.error("background tasks did not stop within %.0fs: %s",
                         timeout, ", ".join(sorted(stuck)))
        return stuck

    # ── introspection ───────────────────────────────────────────────────
    def get(self, name: str) -> asyncio.Task | None:
        return self._tasks.get(name)

    def names(self) -> list[str]:
        return sorted(self._tasks)

    def stats(self) -> dict:
        return {n: {**self._stats.get(n, {}), "running": not t.done()}
                for n, t in self._tasks.items()}


def named_loop(fn, *args, **kwargs):
    """Zero-arg factory for ``fn(*args)`` that keeps ``fn.__name__`` — lease
    telemetry (workers.base) keys per-loop stats by the factory's name."""
    def factory():
        return fn(*args, **kwargs)
    factory.__name__ = getattr(fn, "__name__", "loop")
    factory.__qualname__ = factory.__name__
    return factory


# Process-wide registry used by server.py (startup/shutdown) and read by
# GET /api/ops/runtime-stats.
REGISTRY = TaskRegistry()
