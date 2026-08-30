"""Release Canary Mode (iter-159) — ONE designated demo account runs each
new release ahead of the fleet. Its guard-block rate is continuously
compared against the rest of the fleet; a material divergence AUTO-HALTS
the canary account's bots and raises a critical ops alert."""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("release.canary")

STATE_ID = "release_canary"
WINDOW_HOURS = 24
MIN_CANARY_DECISIONS = 20    # judge only with enough evidence
MIN_CANARY_EXECUTIONS = 10   # execution-health dimension needs fewer
BLOCK_RATE_TOLERANCE = 0.25  # canary may exceed fleet block-rate by ≤25pp
RATE_RATIO_MAX = 3.0         # …and by ≤3× the fleet rate (STRICTER wins)
UNHEALTHY_EXEC_STATUSES = ("failed", "unknown")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_state(db) -> dict:
    st = await db.platform_state.find_one({"_id": STATE_ID}) or {}
    st.pop("_id", None)
    return {"enabled": bool(st.get("enabled")), **st}


async def enable(db, account_id: str, actor: str) -> dict:
    from broker_env import broker_environment
    from route_utils import parse_object_id
    from soak_campaign import release_fingerprint
    acc = await db.accounts.find_one({"_id": parse_object_id(account_id)})
    if not acc:
        raise ValueError("account not found")
    env = broker_environment(acc)
    if env == "LIVE":
        if str(acc.get("account_type") or "") != "demo":
            raise ValueError("canary account must be DEMO/PAPER — a canary "
                             "runs the new release ahead of the fleet, "
                             "never with live capital")
        env = "DEMO"
    st = {"enabled": True, "account_id": str(acc["_id"]),
          "account_name": acc.get("display_name"),
          "environment": env,
          "release": release_fingerprint(),
          "activated_at": _now(), "activated_by": actor,
          "halted": False, "halt_reason": None}
    await db.platform_state.update_one({"_id": STATE_ID},
                                       {"$set": st}, upsert=True)
    logger.info("release canary ENABLED on account %s (%s)",
                st["account_id"], env)
    return st


async def disable(db, actor: str) -> dict:
    await db.platform_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"enabled": False, "disabled_at": _now(),
                  "disabled_by": actor}}, upsert=True)
    return {"enabled": False}


async def block_rates(db, account_id: str,
                      hours: int = WINDOW_HOURS) -> dict:
    since = (datetime.now(timezone.utc)
             - timedelta(hours=hours)).isoformat()

    async def _bucket(match: dict) -> dict:
        total = await db.pamm_risk_decisions.count_documents(match)
        blocked = await db.pamm_risk_decisions.count_documents(
            {**match, "authorized": False})
        return {"decisions": total, "blocked": blocked,
                "block_rate": round(blocked / total, 3) if total else 0.0}

    base = {"at": {"$gte": since}}
    return {"window_hours": hours,
            "canary": await _bucket({**base, "account_id": account_id}),
            "fleet": await _bucket({**base,
                                    "account_id": {"$ne": account_id}})}


def divergence_verdict(canary: dict, fleet: dict,
                       min_decisions: int = MIN_CANARY_DECISIONS) -> dict:
    """Pure — material-divergence check, judged only with enough evidence.

    Review P0: "+25pp" AND "≤3× fleet" are BOTH limits, so the effective
    boundary is the STRICTER (smaller) of the two — never the looser.
    A quiet fleet (0% blocks) therefore yields a 0% boundary: a canary
    that blocks at all, with enough evidence, is divergent by definition.
    """
    n = int(canary.get("decisions") or 0)
    if n < min_decisions:
        return {"diverged": False, "judged": False,
                "reason": f"insufficient evidence ({n}/{min_decisions} "
                          "canary observations in window)"}
    c_rate = float(canary.get("block_rate") or 0.0)
    f_rate = float(fleet.get("block_rate") or 0.0)
    threshold = min(1.0, f_rate + BLOCK_RATE_TOLERANCE,
                    f_rate * RATE_RATIO_MAX)
    if c_rate > threshold:
        return {"diverged": True, "judged": True,
                "reason": (f"canary block-rate {c_rate:.0%} vs fleet "
                           f"{f_rate:.0%} exceeds threshold "
                           f"{threshold:.0%} (stricter of +25pp / 3×)")}
    return {"diverged": False, "judged": True,
            "reason": (f"canary {c_rate:.0%} within threshold "
                       f"{threshold:.0%} (fleet {f_rate:.0%})")}


async def execution_failure_rates(db, account_id: str,
                                  hours: int = WINDOW_HOURS) -> dict:
    """Second canary dimension (review P1): execution health — the share
    of executions ending failed/UNKNOWN, canary vs fleet."""
    since = (datetime.now(timezone.utc)
             - timedelta(hours=hours)).isoformat()

    async def _bucket(match: dict) -> dict:
        total = await db.execution_intents.count_documents(match)
        bad = await db.execution_intents.count_documents(
            {**match, "status": {"$in": list(UNHEALTHY_EXEC_STATUSES)}})
        return {"decisions": total, "blocked": bad,
                "block_rate": round(bad / total, 3) if total else 0.0}

    base = {"created_at": {"$gte": since}}
    return {"window_hours": hours,
            "canary": await _bucket({**base, "account_id": account_id}),
            "fleet": await _bucket({**base,
                                    "account_id": {"$ne": account_id}})}


async def dimension_verdicts(db, account_id: str) -> tuple:
    """Review P1 — canary health is MULTIDIMENSIONAL: guard-block rate
    alone must not decide a release. Returns (dimensions, overall)."""
    rates = await block_rates(db, account_id)
    execs = await execution_failure_rates(db, account_id)
    dims = [
        {"dimension": "guard_block_rate",
         **divergence_verdict(rates["canary"], rates["fleet"]),
         "rates": rates},
        {"dimension": "execution_failure_rate",
         **divergence_verdict(execs["canary"], execs["fleet"],
                              min_decisions=MIN_CANARY_EXECUTIONS),
         "rates": execs},
    ]
    diverged = [d for d in dims if d["diverged"]]
    overall = {"diverged": bool(diverged),
               "judged": any(d["judged"] for d in dims),
               "reason": "; ".join(
                   f"{d['dimension']}: {d['reason']}"
                   for d in (diverged or dims))}
    return dims, overall


async def _halt(db, st: dict, reason: str, rates: dict) -> dict:
    res = await db.bot_configs.update_many(
        {"account_id": st["account_id"], "active": True},
        {"$set": {"active": False, "canary_halted": True}})
    await db.platform_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"halted": True, "halt_reason": reason,
                  "halted_at": _now(), "halt_rates": rates,
                  "halted_configs": int(res.modified_count or 0)}})
    logger.warning("release canary HALTED: %s", reason)
    try:
        from alerting import raise_alert
        await raise_alert(db, "canary_halt", "critical",
                          f"Release canary auto-halted: {reason}",
                          dedup_key="release_canary_halt")
    except Exception as e:  # noqa: BLE001
        logger.warning("canary halt alert failed: %s", e)
    return {"halted": True, "halt_reason": reason,
            "configs_deactivated": int(res.modified_count or 0)}


async def evaluate(db) -> dict:
    """Advance/halt check — called by the background tracker sweep."""
    st = await get_state(db)
    if not st.get("enabled"):
        return {"evaluated": False, "enabled": False}
    if st.get("halted"):
        return {"evaluated": False, "enabled": True, "halted": True,
                "halt_reason": st.get("halt_reason")}
    dims, verdict = await dimension_verdicts(db, st["account_id"])
    rates = next(d["rates"] for d in dims
                 if d["dimension"] == "guard_block_rate")
    if verdict["diverged"]:
        halt = await _halt(db, st, verdict["reason"],
                           {"dimensions": dims})
        return {"evaluated": True, "rates": rates, "verdict": verdict,
                "dimensions": dims, **halt}
    await db.platform_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"last_evaluated_at": _now(), "last_rates": rates,
                  "last_verdict": verdict, "last_dimensions": dims}})
    return {"evaluated": True, "halted": False, "rates": rates,
            "verdict": verdict, "dimensions": dims}


async def resume(db, actor: str) -> dict:
    st = await get_state(db)
    if not st.get("enabled"):
        raise ValueError("canary mode is not enabled")
    res = await db.bot_configs.update_many(
        {"account_id": st.get("account_id"), "canary_halted": True},
        {"$set": {"active": True}, "$unset": {"canary_halted": ""}})
    await db.platform_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"halted": False, "halt_reason": None,
                  "resumed_at": _now(), "resumed_by": actor}})
    logger.info("release canary RESUMED by %s", actor)
    return {"resumed": True,
            "configs_reactivated": int(res.modified_count or 0)}


async def status(db) -> dict:
    st = await get_state(db)
    if not st.get("enabled"):
        return {"enabled": False, **st}
    dims, verdict = await dimension_verdicts(db, st["account_id"])
    rates = next(d["rates"] for d in dims
                 if d["dimension"] == "guard_block_rate")
    return {**st, "rates": rates, "verdict": verdict, "dimensions": dims,
            "thresholds": {"min_canary_decisions": MIN_CANARY_DECISIONS,
                           "min_canary_executions": MIN_CANARY_EXECUTIONS,
                           "block_rate_tolerance": BLOCK_RATE_TOLERANCE,
                           "rate_ratio_max": RATE_RATIO_MAX,
                           "boundary_rule": "stricter of +25pp / 3× fleet",
                           "window_hours": WINDOW_HOURS}}
