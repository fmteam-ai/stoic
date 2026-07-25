"""Correction #6 — automatic authority demotion ladder.

    autonomous_live → supervised_live   health deterioration (< 60 sustained)
    supervised_live → defensive         severe deterioration (< 40)
    any live mode   → observe           broker-truth uncertainty

Demotions are instant, audited, alerted and version-recorded. Recovery is
NEVER automatic: promotion back requires a stable green period
(GREEN_HOURS of healthy samples) AND the explicit step-up-approved
promotion gate (enforced in operational_modes.promotion_gate).
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("auto-demotion")

AUTHORITY = {"autonomous_live": 3, "supervised_live": 2,
             "defensive": 1, "observe": 0}
SUSTAIN_MINUTES = 15
SEVERE = 40
GREEN_HOURS = 24
MIN_GREEN_SAMPLES = 10


def _aware(dt):
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def evaluate_ceiling(health: dict, sustained_below_threshold: bool) -> dict:
    """Pure ladder evaluation → {ceiling, level, reason} (ceiling None =
    no demotion required)."""
    from shadow_health import THRESHOLD
    comps = health.get("components") or {}
    overall = health.get("overall")
    if (comps.get("broker_stability") in (None, 0)
            or comps.get("synchronization") in (None, 0)):
        return {"ceiling": "observe", "level": 0,
                "reason": "broker-truth uncertainty — stale or unknown "
                          "broker state, entries frozen"}
    if overall is not None and overall < SEVERE:
        return {"ceiling": "defensive", "level": 1,
                "reason": f"severe health deterioration "
                          f"({overall} < {SEVERE})"}
    if overall is not None and overall < THRESHOLD \
            and sustained_below_threshold:
        return {"ceiling": "supervised_live", "level": 2,
                "reason": f"health below threshold ({overall} < "
                          f"{THRESHOLD}) sustained ≥ {SUSTAIN_MINUTES} min"}
    return {"ceiling": None, "level": 3, "reason": "healthy"}


async def sweep_user(db, user_id: str) -> dict:
    """Sample health, evaluate the ceiling and demote any active config
    holding more authority than the ceiling allows."""
    from shadow_health import THRESHOLD, health_score
    hs = await health_score(db, user_id)
    now = datetime.now(timezone.utc)
    await db.health_samples.insert_one({
        "user_id": user_id, "overall": hs.get("overall"),
        "fail_closed": hs.get("fail_closed"),
        "missing_components": hs.get("missing_components"), "at": now})
    since = now - timedelta(minutes=SUSTAIN_MINUTES)
    recent = [s async for s in db.health_samples.find(
        {"user_id": user_id, "at": {"$gte": since}})]
    sustained = (len(recent) >= 2
                 and all((s.get("overall") or 0) < THRESHOLD
                         for s in recent))
    verdict = evaluate_ceiling(hs, sustained)
    demotions = []
    if verdict["ceiling"] is not None:
        above = [m for m, lvl in AUTHORITY.items()
                 if lvl > verdict["level"]]
        async for cfg in db.bot_configs.find(
                {"user_id": user_id, "active": True,
                 "operational_mode": {"$in": above}}):
            frm = cfg.get("operational_mode")
            await db.bot_configs.update_one(
                {"_id": cfg["_id"]},
                {"$set": {"operational_mode": verdict["ceiling"],
                          "mode_demoted_at": now.isoformat(),
                          "mode_demotion_reason": verdict["reason"]}})
            updated = await db.bot_configs.find_one({"_id": cfg["_id"]})
            from config_promotion import record_version
            await record_version(db, updated, label="auto-demotion",
                                 source="auto_demotion")
            demotions.append({"config_id": str(cfg["_id"]),
                              "account_id": cfg.get("account_id"),
                              "from": frm, "to": verdict["ceiling"]})
        if demotions:
            await db.mode_demotions.insert_one({
                "user_id": user_id, "reason": verdict["reason"],
                "ceiling": verdict["ceiling"],
                "health_overall": hs.get("overall"),
                "demotions": demotions, "automatic": True, "at": now})
            await db.audit_log.insert_one({
                "user_id": user_id, "action": "auto_mode_demotion",
                "detail": {"reason": verdict["reason"],
                           "demotions": demotions},
                "step_up_verified": False, "at": now})
            try:
                from alerting import raise_alert
                await raise_alert(
                    db, "auto_mode_demotion", "critical",
                    f"Auto-demotion: {len(demotions)} config(s) → "
                    f"{verdict['ceiling']} — {verdict['reason']}. Recovery "
                    f"needs {GREEN_HOURS}h green + explicit promotion.",
                    dedup_key=f"auto_demotion_{user_id}")
            except Exception as e:  # noqa: BLE001
                logger.warning("auto-demotion alert failed: %s", e)
            logger.warning("auto-demotion user=%s → %s (%s): %s", user_id,
                           verdict["ceiling"], verdict["reason"], demotions)
    return {"verdict": verdict, "demoted": demotions,
            "health": hs.get("overall")}


async def recovery_status(db, user_id: str) -> dict:
    """Recovery to higher authority needs GREEN_HOURS of continuous healthy
    samples after the last automatic demotion — plus the explicit,
    step-up-approved promotion (never automatic)."""
    from shadow_health import THRESHOLD
    last = await db.mode_demotions.find_one(
        {"user_id": user_id, "automatic": True}, sort=[("at", -1)])
    if not last:
        return {"applicable": False, "eligible": True,
                "reason": "no automatic demotion on record"}
    now = datetime.now(timezone.utc)
    last_at = _aware(last.get("at"))
    since = now - timedelta(hours=GREEN_HOURS)
    samples = [s async for s in db.health_samples.find(
        {"user_id": user_id, "at": {"$gte": since}})]
    unhealthy = [s for s in samples
                 if (s.get("overall") or 0) < THRESHOLD
                 or s.get("fail_closed")]
    hours_since = ((now - last_at).total_seconds() / 3600
                   if last_at else 0.0)
    green = (len(samples) >= MIN_GREEN_SAMPLES and not unhealthy
             and hours_since >= GREEN_HOURS)
    if green:
        reason = (f"{GREEN_HOURS}h green period complete "
                  f"({len(samples)} healthy samples) — explicit "
                  f"step-up-approved promotion required")
    else:
        reason = (f"stable green period required: {len(unhealthy)} "
                  f"unhealthy of {len(samples)} samples in the last "
                  f"{GREEN_HOURS}h · {hours_since:.1f}h since demotion "
                  f"(need {GREEN_HOURS}h and ≥{MIN_GREEN_SAMPLES} "
                  f"healthy samples)")
    return {"applicable": True, "eligible": green,
            "last_demotion_at": str(last.get("at")),
            "last_demotion_reason": last.get("reason"),
            "samples": len(samples), "unhealthy_samples": len(unhealthy),
            "hours_since_demotion": round(hours_since, 1),
            "required_green_hours": GREEN_HOURS, "reason": reason}
