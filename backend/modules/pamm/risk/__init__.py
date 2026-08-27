"""PAMM risk gate (Phase 9) — the broker executes, STOIC decides whether
trading is ALLOWED. Full engine lives in limits.py (caps/dd/exposure/
correlation) and news.py (economic-calendar blackout)."""
from modules.pamm.events import emit_event
from modules.pamm.risk.limits import (DEFAULT_LIMITS, evaluate_program,
                                      get_limits, run_risk_check,
                                      validate_limits_patch)
from modules.pamm.risk.news import get_calendar, news_blackout_status

__all__ = ["trading_allowed", "emergency_stop", "run_risk_check",
           "evaluate_program", "get_limits", "validate_limits_patch",
           "DEFAULT_LIMITS", "get_calendar", "news_blackout_status"]


async def trading_allowed(db, program: dict) -> tuple[bool, str]:
    if program.get("emergency_stop"):
        return False, "emergency_stop"
    breach = program.get("risk_breach")
    if breach:
        return False, f"risk_breach:{','.join(breach.get('limits', []))}"
    if program.get("trading") == "paused":
        return False, "paused"
    if program.get("status") != "active":
        return False, f"program {program.get('status')}"
    news_cfg = get_limits(program)["news_filter"]
    if news_cfg.get("enabled"):
        news = await news_blackout_status(db, news_cfg)
        if news["active"]:
            return False, f"news_blackout:{news['event']['title']}"
    return True, "ok"


async def emergency_stop(db, program: dict, actor_id: str,
                         reason: str) -> dict:
    from services.broker_gateway.manager_api import pause_program
    await pause_program(db, program, actor_id, reason=f"EMERGENCY: {reason}")
    await db.pamm_programs.update_one(
        {"program_id": program["program_id"]},
        {"$set": {"emergency_stop": True}})
    await emit_event(db, "EmergencyStop",
                     {"program_id": program["program_id"], "actor": actor_id,
                      "reason": reason[:200]})
    return {"program_id": program["program_id"], "emergency_stop": True}
