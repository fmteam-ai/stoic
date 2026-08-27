"""Degraded Intelligence Mode (v59 #7) — formal per-subsystem behavior
when an AI component fails, instead of ad-hoc fail-open/fail-closed.
Memory failure → continue; meta failure → reduced risk; portfolio brain
failure → deterministic limits; position-truth/risk/authority failure →
no new exposure. Global state: NORMAL / DEGRADED_INTELLIGENCE."""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("degraded.intelligence")

POLICY = {
    "market_memory": {"fallback": "continue without memory",
                      "risk_multiplier": 1.0, "critical": False},
    "meta_decision": {"fallback": "deterministic gates + reduced risk",
                      "risk_multiplier": 0.5, "critical": True},
    "uncertainty": {"fallback": "reduce risk",
                    "risk_multiplier": 0.6, "critical": True},
    "portfolio_brain": {"fallback": "deterministic portfolio limits "
                                    "(0.5x lot cap)",
                        "risk_multiplier": 0.5, "critical": True},
    "position_truth": {"fallback": "CLOSE_ONLY",
                       "risk_multiplier": 0.0, "critical": True},
    "risk_engine": {"fallback": "no new exposure",
                    "risk_multiplier": 0.0, "critical": True},
    "execution_authority": {"fallback": "no new exposure",
                            "risk_multiplier": 0.0, "critical": True},
    "transaction_costs": {"fallback": "static conservative cost floor",
                          "risk_multiplier": 0.9, "critical": False},
    "strategy_decay": {"fallback": "continue without health signal",
                       "risk_multiplier": 1.0, "critical": False},
}
FAIL_THRESHOLD = 1   # a single live-path failure already degrades
_mem: dict = {}      # subsystem → consecutive failures (fast local hint)
_redis = None
_redis_failed_at = 0.0
_REDIS_RETRY_S = 30
_FAIL_KEY_TTL_S = 6 * 3600


async def _get_redis():
    """Shared health counters across instances when REDIS_URL is set;
    Mongo remains the persisted record either way."""
    global _redis, _redis_failed_at
    import os
    import time
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    if _redis is not None:
        return _redis
    if time.time() - _redis_failed_at < _REDIS_RETRY_S:
        return None
    try:
        import redis.asyncio as aioredis
        _redis = aioredis.from_url(url, socket_timeout=1,
                                   socket_connect_timeout=1)
        await _redis.ping()
        return _redis
    except Exception as e:  # noqa: BLE001
        logger.warning("degraded-intelligence redis unavailable: %s", e)
        _redis = None
        _redis_failed_at = time.time()
        return None


def multiplier_for(subsystem: str) -> float:
    return float(POLICY.get(subsystem, {}).get("risk_multiplier", 0.5))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _shared_failures(db, subsystem: str) -> int:
    """Cross-instance consecutive-failure count: Redis first, Mongo doc
    as the fallback source of truth."""
    r = await _get_redis()
    if r is not None:
        try:
            v = await r.get(f"di:fails:{subsystem}")
            return int(v or 0)
        except Exception:  # noqa: BLE001
            pass
    doc = await db.intelligence_health.find_one(
        {"_id": subsystem}, {"consecutive_failures": 1, "ok": 1})
    if not doc or doc.get("ok", True):
        return 0
    return int(doc.get("consecutive_failures") or 0)


async def report(db, subsystem: str, ok: bool,
                 error: str | None = None) -> None:
    """Distributed + state-transition-aware: failure counters are shared
    via Redis (when configured) with Mongo as the persisted record, so a
    recovery observed on ANY instance clears the degradation everywhere."""
    if ok:
        prev = _mem.get(subsystem)
        if prev == 0:            # this process already confirmed healthy
            return
        if prev is None:         # unknown here — consult the shared store
            prev = await _shared_failures(db, subsystem)
        _mem[subsystem] = 0
        if not prev:
            return
        r = await _get_redis()
        if r is not None:
            try:
                await r.delete(f"di:fails:{subsystem}")
            except Exception:  # noqa: BLE001
                pass
        try:
            await db.intelligence_health.update_one(
                {"_id": subsystem},
                {"$set": {"ok": True, "consecutive_failures": 0,
                          "last_ok_at": _now()}}, upsert=True)
            logger.info("subsystem %s RECOVERED", subsystem)
        except Exception as e:  # noqa: BLE001
            logger.debug("degraded report failed: %s", e)
        return
    shared = _mem.get(subsystem, 0) + 1
    r = await _get_redis()
    if r is not None:
        try:
            shared = int(await r.incr(f"di:fails:{subsystem}"))
            await r.expire(f"di:fails:{subsystem}", _FAIL_KEY_TTL_S)
        except Exception:  # noqa: BLE001
            pass
    _mem[subsystem] = shared
    try:
        await db.intelligence_health.update_one(
            {"_id": subsystem},
            {"$set": {"ok": False, "last_error": str(error or "")[:400],
                      "last_failure_at": _now(),
                      "consecutive_failures": shared}}, upsert=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("degraded report failed: %s", e)
    if shared == FAIL_THRESHOLD:
        pol = POLICY.get(subsystem, {})
        logger.warning(
            "DEGRADED_INTELLIGENCE: %s failing → fallback '%s' "
            "(risk x%s)", subsystem, pol.get("fallback", "reduce risk"),
            pol.get("risk_multiplier", 0.5))


async def status(db) -> dict:
    subsystems = {}
    async for doc in db.intelligence_health.find({}):
        name = str(doc["_id"])
        failing = (not doc.get("ok", True)
                   and int(doc.get("consecutive_failures") or 0)
                   >= FAIL_THRESHOLD)
        subsystems[name] = {
            "ok": bool(doc.get("ok", True)), "failing": failing,
            "consecutive_failures": int(
                doc.get("consecutive_failures") or 0),
            "last_error": doc.get("last_error"),
            "last_failure_at": doc.get("last_failure_at"),
            "last_ok_at": doc.get("last_ok_at"),
            "policy": POLICY.get(name, {"fallback": "reduce risk",
                                        "risk_multiplier": 0.5})}
    for name, pol in POLICY.items():
        subsystems.setdefault(name, {"ok": True, "failing": False,
                                     "consecutive_failures": 0,
                                     "policy": pol})
    failing = [n for n, s in subsystems.items() if s["failing"]]
    critical = [n for n in failing
                if POLICY.get(n, {}).get("critical")]
    mode = "DEGRADED_INTELLIGENCE" if failing else "NORMAL"
    return {"mode": mode, "failing": failing,
            "critical_failing": critical,
            "subsystems": subsystems, "at": _now()}
