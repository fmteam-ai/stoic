"""PAMM risk gate (Phase 9) — the broker executes, STOIC decides whether
trading is ALLOWED and at what size. Full engine: limits.py (caps/dd/
exposure/correlation), news.py (calendar blackout), states.py (op-state
hierarchy), verdict.py (APPROVE/REDUCE/REJECT)."""
from modules.pamm.events import emit_event
from modules.pamm.risk.limits import (DEFAULT_LIMITS, evaluate_program,
                                      get_limits, run_risk_check,
                                      validate_limits_patch)
from modules.pamm.risk.news import get_calendar, news_blackout_status
from modules.pamm.risk.states import (OP_STATES, blocks_new_trades,
                                      op_state_of, set_op_state)
from modules.pamm.risk.verdict import trade_verdict

__all__ = ["trading_allowed", "emergency_stop", "run_risk_check",
           "evaluate_program", "get_limits", "validate_limits_patch",
           "DEFAULT_LIMITS", "get_calendar", "news_blackout_status",
           "OP_STATES", "op_state_of", "set_op_state", "trade_verdict"]


async def trading_allowed(db, program: dict) -> tuple[bool, str]:
    state = op_state_of(program)
    if state == "locked":
        return False, "locked"
    breach = program.get("risk_breach")
    if breach:
        return False, f"risk_breach:{','.join(breach.get('limits', []))}"
    if program.get("emergency_stop"):
        return False, "emergency_stop"
    if blocks_new_trades(state):
        return False, state
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
    """Escalates to EMERGENCY_FLATTEN: pause + flatten + estop flag."""
    await set_op_state(db, program, "emergency_flatten", actor_id,
                       reason=f"EMERGENCY: {reason}", source="human")
    await emit_event(db, "EmergencyStop",
                     {"program_id": program["program_id"], "actor": actor_id,
                      "reason": reason[:200]})
    return {"program_id": program["program_id"], "emergency_stop": True,
            "op_state": "emergency_flatten"}
