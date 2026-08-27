"""PAMM risk gate (Milestone-1 foundation; full engine in Milestone 2).
The broker executes — STOIC decides whether trading is ALLOWED."""
from modules.pamm.events import emit_event


async def trading_allowed(db, program: dict) -> tuple[bool, str]:
    if program.get("emergency_stop"):
        return False, "emergency_stop"
    if program.get("trading") == "paused":
        return False, "paused"
    if program.get("status") != "active":
        return False, f"program {program.get('status')}"
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
