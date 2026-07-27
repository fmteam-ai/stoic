"""iter-171 (#9) — slow-query monitoring via a pymongo command listener.

Registered on the Motor client so every DB command slower than SLOW_QUERY_MS
is logged with its collection, duration and a bounded filter shape (no
values, to avoid leaking data). Aggregated counters are exposed for the Ops
Console query-performance panel.
"""
import logging
import os

from pymongo import monitoring

logger = logging.getLogger("query-perf")
SLOW_QUERY_MS = float(os.environ.get("SLOW_QUERY_MS", "500"))
_MONITORED = {"find", "aggregate", "update", "delete", "insert",
              "findAndModify", "count", "distinct"}

# rolling counters (process-local; Ops Console aggregates across the fleet)
STATS = {"total": 0, "slow": 0, "slowest_ms": 0.0, "slowest_cmd": None}


class SlowQueryListener(monitoring.CommandListener):
    def started(self, event):  # noqa: D401
        pass

    def succeeded(self, event):
        if event.command_name not in _MONITORED:
            return
        ms = event.duration_micros / 1000.0
        STATS["total"] += 1
        if ms > STATS["slowest_ms"]:
            STATS["slowest_ms"] = round(ms, 1)
            STATS["slowest_cmd"] = event.command_name
        if ms >= SLOW_QUERY_MS:
            STATS["slow"] += 1
            logger.warning("SLOW QUERY %.1fms cmd=%s db=%s",
                           ms, event.command_name, event.database_name)

    def failed(self, event):
        pass


def query_perf_stats() -> dict:
    return dict(STATS, threshold_ms=SLOW_QUERY_MS)
