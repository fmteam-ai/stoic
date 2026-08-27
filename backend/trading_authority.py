"""Global Trading Authority (v56 §16) — ONE first-class entity that
answers "may STOIC open new exposure right now?". Computed as the MOST
RESTRICTIVE level across all domains (platform, account, broker,
infrastructure, risk, PAMM, execution, position truth). Strategies never
decide executability — the authority does, at the single execution
choke point (MT5BridgeEngine.execute).

Levels (ordered): FULL < REDUCED < CLOSE_ONLY < PAUSED < EMERGENCY < LOCKED
Enforcement: platform + account domains BLOCK at the choke point; the
remaining domains are computed and surfaced (dashboard / ops) and feed
their own native enforcement paths (PAMM op-states, safety guardian).
A domain whose state cannot be evaluated degrades to CLOSE_ONLY — the
system never infers safety from missing information."""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("trading.authority")

LEVELS = ["FULL", "REDUCED", "CLOSE_ONLY", "PAUSED", "EMERGENCY", "LOCKED"]
_UNAVAILABLE = {"level": "CLOSE_ONLY",
                "reason": "domain state unavailable — never infer safety"}


def level_severity(level: str) -> int:
    return LEVELS.index(level)


def worst(*levels: str) -> str:
    return max(levels, key=level_severity)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds)).isoformat()


async def platform_domain(db) -> dict:
    doc = await db.platform_state.find_one({"_id": "trading_authority"})
    if doc and doc.get("level") in LEVELS and doc["level"] != "FULL":
        return {"level": doc["level"],
                "reason": doc.get("reason") or "platform override",
                "set_by": doc.get("set_by")}
    return {"level": "FULL", "reason": "no platform restriction"}


async def account_domain(db, account: dict | None) -> dict:
    if not account:
        return {"level": "FULL", "reason": "no account context"}
    lvl = account.get("trading_authority")
    if lvl in LEVELS and lvl != "FULL":
        return {"level": lvl, "reason": "account-level restriction"}
    return {"level": "FULL", "reason": "account unrestricted"}


async def infrastructure_domain(db, account: dict | None = None) -> dict:
    if account is not None:
        hb = str(account.get("last_heartbeat") or "")
        if hb and hb >= _ago(180):
            return {"level": "FULL", "reason": "terminal heartbeat fresh"}
        if hb:
            return {"level": "REDUCED",
                    "reason": "terminal heartbeat stale (>180s)"}
        return {"level": "REDUCED", "reason": "terminal never connected"}
    n = await db.accounts.count_documents(
        {"last_heartbeat": {"$gte": _ago(600)}})
    if n:
        return {"level": "FULL", "reason": f"{n} live terminal(s)"}
    return {"level": "REDUCED",
            "reason": "no live terminal heartbeats in 10m"}


async def broker_domain(db) -> dict:
    inc = await db.pamm_incidents.count_documents(
        {"status": "open", "type": "flatten_failed"})
    if inc:
        return {"level": "CLOSE_ONLY",
                "reason": f"{inc} open flatten-failed incident(s)"}
    down = await db.broker_partners.count_documents(
        {"status": {"$nin": ["active"]}})
    if down:
        return {"level": "REDUCED",
                "reason": f"{down} broker partner(s) inactive"}
    return {"level": "FULL", "reason": "broker links healthy"}


async def risk_domain(db) -> dict:
    n = await db.safety_blocks.count_documents(
        {"blocked_at": {"$gte": _ago(1800)}})
    if n:
        return {"level": "REDUCED",
                "reason": f"safety guardian refused {n} trade(s) in 30m"}
    return {"level": "FULL", "reason": "no recent safety refusals"}


_PAMM_MAP = {"running": "FULL", "risk_reduced": "REDUCED",
             "new_trades_paused": "CLOSE_ONLY",
             "broker_uncertain": "CLOSE_ONLY",
             "close_risk_only": "CLOSE_ONLY",
             "emergency_flatten": "EMERGENCY", "locked": "LOCKED"}


async def pamm_domain(db) -> dict:
    lvl, why = "FULL", "all programs running"
    async for p in db.pamm_programs.find({"status": "active"},
                                         {"op_state": 1, "name": 1}):
        mapped = _PAMM_MAP.get(p.get("op_state") or "running", "CLOSE_ONLY")
        if level_severity(mapped) > level_severity(lvl):
            lvl = mapped
            why = (f"program '{p.get('name')}' is "
                   f"{(p.get('op_state') or '').replace('_', ' ')}")
    return {"level": lvl, "reason": why}


async def execution_domain(db) -> dict:
    n = await db.execution_intents.count_documents({"status": "unknown"})
    if n:
        return {"level": "REDUCED",
                "reason": f"{n} execution(s) UNKNOWN — broker "
                          f"reconciliation pending"}
    return {"level": "FULL", "reason": "all executions reconciled"}


async def position_truth_domain(db) -> dict:
    n = await db.pamm_incidents.count_documents(
        {"status": "open", "type": "position_drift"})
    if n:
        return {"level": "CLOSE_ONLY",
                "reason": f"{n} open position drift incident(s)"}
    return {"level": "FULL", "reason": "positions reconciled"}


_DOMAINS = {
    "platform": platform_domain,
    "broker": broker_domain,
    "risk": risk_domain,
    "pamm": pamm_domain,
    "execution": execution_domain,
    "position_truth": position_truth_domain,
}


async def compute_authority(db, account: dict | None = None) -> dict:
    domains = {}
    for name, fn in _DOMAINS.items():
        try:
            domains[name] = await fn(db)
        except Exception as e:
            logger.error("authority domain %s failed: %s", name, e)
            domains[name] = dict(_UNAVAILABLE)
    try:
        domains["infrastructure"] = await infrastructure_domain(db, account)
    except Exception as e:
        logger.error("authority domain infrastructure failed: %s", e)
        domains["infrastructure"] = dict(_UNAVAILABLE)
    try:
        domains["account"] = await account_domain(db, account)
    except Exception:
        domains["account"] = dict(_UNAVAILABLE)
    effective = worst(*(d["level"] for d in domains.values()))
    enforced = worst(domains["platform"]["level"],
                     domains["account"]["level"])
    return {"level": effective, "enforced_level": enforced,
            "restricted": effective != "FULL", "domains": domains,
            "reasons": [f"{k}: {v['reason']}" for k, v in domains.items()
                        if v["level"] != "FULL"],
            "computed_at": _now()}


async def enforce_new_trade(db, account: dict | None = None) -> dict:
    """Blocking gate at the execution choke point. CLOSE_ONLY and above
    refuse new exposure; REDUCED halves requested volume."""
    p = await platform_domain(db)
    a = await account_domain(db, account)
    lvl = worst(p["level"], a["level"])
    reasons = [d["reason"] for d in (p, a) if d["level"] != "FULL"]
    if level_severity(lvl) >= level_severity("CLOSE_ONLY"):
        return {"ok": False, "level": lvl, "reasons": reasons}
    if lvl == "REDUCED":
        return {"ok": True, "level": lvl, "reduce_factor": 0.5,
                "reasons": reasons}
    return {"ok": True, "level": "FULL", "reasons": []}


async def set_platform_level(db, level: str, reason: str,
                             actor: str) -> dict:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    prev = await db.platform_state.find_one({"_id": "trading_authority"})
    prev_level = (prev or {}).get("level") or "FULL"
    doc = {"level": level, "reason": str(reason or "")[:300],
           "set_by": actor, "set_at": _now(), "previous": prev_level}
    await db.platform_state.update_one({"_id": "trading_authority"},
                                       {"$set": doc}, upsert=True)
    await db.authority_audit.insert_one(
        {"at": _now(), "actor": actor, "from": prev_level, "to": level,
         "reason": doc["reason"]})
    logger.warning("PLATFORM TRADING AUTHORITY %s → %s by %s (%s)",
                   prev_level, level, actor, reason)
    return doc
