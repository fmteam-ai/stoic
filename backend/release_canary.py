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
BLOCK_RATE_TOLERANCE = 0.25  # canary may exceed fleet block-rate by ≤25pp
RATE_RATIO_MAX = 3.0         # …and by ≤3× the fleet rate (worst of the two)


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
    """Pure — material-divergence check, judged only with enough evidence."""
    n = int(canary.get("decisions") or 0)
    if n < min_decisions:
        return {"diverged": False, "judged": False,
                "reason": f"insufficient evidence ({n}/{min_decisions} "
                          "canary guard decisions in window)"}
    c_rate = float(canary.get("block_rate") or 0.0)
    f_rate = float(fleet.get("block_rate") or 0.0)
    threshold = min(1.0, max(f_rate + BLOCK_RATE_TOLERANCE,
                             f_rate * RATE_RATIO_MAX))
    if c_rate > threshold:
        return {"diverged": True, "judged": True,
                "reason": (f"canary block-rate {c_rate:.0%} vs fleet "
                           f"{f_rate:.0%} exceeds threshold "
                           f"{threshold:.0%}")}
    return {"diverged": False, "judged": True,
            "reason": (f"canary {c_rate:.0%} within threshold "
                       f"{threshold:.0%} (fleet {f_rate:.0%})")}


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
    rates = await block_rates(db, st["account_id"])
    verdict = divergence_verdict(rates["canary"], rates["fleet"])
    if verdict["diverged"]:
        halt = await _halt(db, st, verdict["reason"], rates)
        return {"evaluated": True, "rates": rates, "verdict": verdict,
                **halt}
    await db.platform_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"last_evaluated_at": _now(), "last_rates": rates,
                  "last_verdict": verdict}})
    return {"evaluated": True, "halted": False, "rates": rates,
            "verdict": verdict}


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
    rates = await block_rates(db, st["account_id"])
    verdict = divergence_verdict(rates["canary"], rates["fleet"])
    return {**st, "rates": rates, "verdict": verdict,
            "thresholds": {"min_canary_decisions": MIN_CANARY_DECISIONS,
                           "block_rate_tolerance": BLOCK_RATE_TOLERANCE,
                           "rate_ratio_max": RATE_RATIO_MAX,
                           "window_hours": WINDOW_HOURS}}
