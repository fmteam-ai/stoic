"""Execution timing (Phase 2) — should this order go out NOW or wait 1-2s?

The EA streams spreads with every heartbeat. We keep a short in-memory
rolling window per account+symbol; when the live spread is materially above
its recent median (a transient widening — news tick, liquidity gap), the
order is briefly delayed and re-checked. Outcomes are persisted so the
broker-intel endpoint can show whether delaying actually helps per broker.

Never vetoes — worst case the order goes out after the max delay.
"""
import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timezone

from pip_utils import base_symbol

logger = logging.getLogger("execution-timing")

WINDOW_SEC = 600
MIN_SAMPLES = 8
ELEVATED_RATIO = 1.3
MAX_DELAY_MS = 2000
RECHECK_STEP_MS = 400

_HIST: dict[tuple, deque] = {}


def record_spread(account_id: str, spreads: dict | None):
    """Hook — called from the bridge heartbeat for every spread snapshot."""
    if not spreads:
        return
    now = time.time()
    for sym, sp in spreads.items():
        try:
            key = (account_id, base_symbol(sym))
            dq = _HIST.setdefault(key, deque(maxlen=240))
            dq.append((now, float(sp)))
        except Exception:
            continue


def _median(vals):
    s = sorted(vals)
    return s[len(s) // 2]


def decide(account_id: str, symbol: str) -> dict:
    """Pure decision from the rolling window."""
    key = (account_id, base_symbol(symbol))
    now = time.time()
    samples = [(t, sp) for t, sp in _HIST.get(key, ())
               if now - t <= WINDOW_SEC]
    if len(samples) < MIN_SAMPLES:
        return {"delay_ms": 0, "reason": "insufficient spread history — send now",
                "samples": len(samples)}
    spread_now = samples[-1][1]
    med = _median([sp for _, sp in samples])
    if med <= 0 or spread_now <= med * ELEVATED_RATIO:
        return {"delay_ms": 0, "spread_now": spread_now, "spread_median": med,
                "reason": f"spread {spread_now:.1f}p ≤ {ELEVATED_RATIO}× median "
                          f"{med:.1f}p — send now"}
    ratio = spread_now / med
    delay = int(min(MAX_DELAY_MS, 600 * ratio))
    return {"delay_ms": delay, "spread_now": spread_now, "spread_median": med,
            "reason": f"spread {spread_now:.1f}p is {ratio:.1f}× the 10-min "
                      f"median {med:.1f}p — wait up to {delay}ms for reversion"}


async def consider_delay(db, account: dict, symbol: str) -> dict:
    """Full flow: decide → (optionally) wait for spread reversion → record
    the outcome. Returns the timing verdict stamped on the signal."""
    acc_id = str(account["_id"])
    verdict = decide(acc_id, symbol)
    if not verdict["delay_ms"]:
        return verdict
    key = (acc_id, base_symbol(symbol))
    before = verdict["spread_now"]
    med = verdict["spread_median"]
    waited = 0
    spread_after = before
    while waited < verdict["delay_ms"]:
        step = min(RECHECK_STEP_MS, verdict["delay_ms"] - waited)
        await asyncio.sleep(step / 1000.0)
        waited += step
        dq = _HIST.get(key)
        if dq:
            spread_after = dq[-1][1]
            if spread_after <= med * 1.1:
                break
    improved = spread_after < before
    verdict.update({"waited_ms": waited, "spread_after": spread_after,
                    "improved": improved})
    try:
        await db.execution_timing_stats.insert_one({
            "account_id": acc_id, "symbol": base_symbol(symbol),
            "spread_before": before, "spread_after": spread_after,
            "spread_median": med, "waited_ms": waited, "improved": improved,
            "at": datetime.now(timezone.utc)})
    except Exception:  # noqa: BLE001
        pass
    logger.info("execution timing acct=%s %s: waited %dms spread %.1f→%.1fp",
                acc_id, symbol, waited, before, spread_after)
    return verdict


async def timing_stats(db, account_id: str) -> dict:
    """Aggregate effectiveness for the broker-intel endpoint."""
    docs = await db.execution_timing_stats.find(
        {"account_id": account_id}).sort("at", -1).to_list(length=200)
    if not docs:
        return {"delays": 0}
    improved = sum(1 for d in docs if d.get("improved"))
    avg_saved = sum((d["spread_before"] - d["spread_after"]) for d in docs) / len(docs)
    return {"delays": len(docs), "improved": improved,
            "improve_rate": round(improved / len(docs), 2),
            "avg_spread_saved_pips": round(avg_saved, 2)}
