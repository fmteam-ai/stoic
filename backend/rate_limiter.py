"""Per-user order rate limiter (kill-switch).

In-memory sliding window — counts trade INSERTs per user over the last
60 seconds and rejects further inserts above the configured cap. Defends
against a runaway code loop accidentally spamming the broker / EA bridge.
"""
import time
from collections import deque
from typing import Deque, Dict
import os


def max_orders_per_minute() -> int:
    return int(os.environ.get("MAX_ORDERS_PER_MIN", "5"))


_buckets: Dict[str, Deque[float]] = {}


def _prune(window: Deque[float], now: float):
    cutoff = now - 60.0
    while window and window[0] < cutoff:
        window.popleft()


def check_and_record(user_id: str) -> dict:
    """Returns {allowed: bool, count_60s: int, limit: int, retry_in_s: float}."""
    limit = max_orders_per_minute()
    now = time.time()
    window = _buckets.setdefault(user_id, deque())
    _prune(window, now)
    if len(window) >= limit:
        retry_in = max(0.0, 60.0 - (now - window[0]))
        return {"allowed": False, "count_60s": len(window),
                "limit": limit, "retry_in_s": round(retry_in, 1)}
    window.append(now)
    return {"allowed": True, "count_60s": len(window),
            "limit": limit, "retry_in_s": 0.0}


def current_usage(user_id: str) -> dict:
    now = time.time()
    window = _buckets.get(user_id, deque())
    _prune(window, now)
    return {"count_60s": len(window), "limit": max_orders_per_minute()}


def reset(user_id: str = None):
    if user_id:
        _buckets.pop(user_id, None)
    else:
        _buckets.clear()
