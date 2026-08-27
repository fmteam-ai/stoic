"""Manager-facing broker operations — pause/resume/master state, with the
PAMM event log recording every action (Phase 14 events)."""
from services.broker_gateway.pamm_api import get_adapter


async def pause_program(db, program: dict, actor_id: str, reason: str = ""):
    adapter = await get_adapter(db, program["partner_id"])
    out = await adapter.pause_trading(program["broker_program_id"])
    await db.pamm_programs.update_one(
        {"program_id": program["program_id"]},
        {"$set": {"trading": "paused"}})
    from modules.pamm.events import emit_event
    await emit_event(db, "StrategyPaused",
                     {"program_id": program["program_id"], "actor": actor_id,
                      "reason": reason[:200]})
    return out


async def resume_program(db, program: dict, actor_id: str):
    adapter = await get_adapter(db, program["partner_id"])
    out = await adapter.resume_trading(program["broker_program_id"])
    await db.pamm_programs.update_one(
        {"program_id": program["program_id"]},
        {"$set": {"trading": "enabled"}})
    from modules.pamm.events import emit_event
    await emit_event(db, "StrategyResumed",
                     {"program_id": program["program_id"], "actor": actor_id})
    return out


async def master_account(db, program: dict) -> dict:
    adapter = await get_adapter(db, program["partner_id"])
    return await adapter.get_master_account(program["broker_program_id"])
