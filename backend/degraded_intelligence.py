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
_mem: dict = {}      # subsystem → consecutive failures (this process)


def multiplier_for(subsystem: str) -> float:
    return float(POLICY.get(subsystem, {}).get("risk_multiplier", 0.5))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def report(db, subsystem: str, ok: bool,
                 error: str | None = None) -> None:
    """State-transition-aware: healthy→healthy never writes."""
    prev = _mem.get(subsystem, 0)
    if ok:
        _mem[subsystem] = 0
        if prev == 0:
            return
        try:
            await db.intelligence_health.update_one(
                {"_id": subsystem},
                {"$set": {"ok": True, "consecutive_failures": 0,
                          "last_ok_at": _now()}}, upsert=True)
            logger.info("subsystem %s RECOVERED", subsystem)
        except Exception as e:  # noqa: BLE001
            logger.debug("degraded report failed: %s", e)
        return
    _mem[subsystem] = prev + 1
    try:
        await db.intelligence_health.update_one(
            {"_id": subsystem},
            {"$set": {"ok": False, "last_error": str(error or "")[:400],
                      "last_failure_at": _now()},
             "$inc": {"consecutive_failures": 1}}, upsert=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("degraded report failed: %s", e)
    if _mem[subsystem] == FAIL_THRESHOLD:
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
