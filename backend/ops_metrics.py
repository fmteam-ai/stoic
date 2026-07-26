"""In-process API latency ring buffer (iter-137) — feeds the Ops Console.
Populated by the request-id middleware; survives only per-process (fine:
the console needs 'now' health, not history)."""
import time
from collections import deque

_SAMPLES = deque(maxlen=1000)  # (epoch_ts, duration_ms, status_code)


def record(duration_ms: float, status_code: int) -> None:
    _SAMPLES.append((time.time(), duration_ms, status_code))


def summary(window_sec: int = 900) -> dict:
    cutoff = time.time() - window_sec
    recent = [(ms, st) for ts, ms, st in _SAMPLES if ts >= cutoff]
    if not recent:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None,
                "error_rate_pct": None, "window_sec": window_sec}
    durs = sorted(ms for ms, _ in recent)
    n = len(durs)
    errors = sum(1 for _, st in recent if st >= 500)
    return {
        "count": n,
        "p50_ms": round(durs[n // 2], 1),
        "p95_ms": round(durs[min(n - 1, int(n * 0.95))], 1),
        "max_ms": round(durs[-1], 1),
        "error_rate_pct": round(errors * 100.0 / n, 2),
        "window_sec": window_sec,
    }
