"""PAMM emergency operating-state hierarchy (review §11).
RUNNING → RISK_REDUCED → NEW_TRADES_PAUSED → CLOSE_RISK_ONLY →
EMERGENCY_FLATTEN → LOCKED.
Automation may only ESCALATE (safer). De-escalation is human-only, and
leaving LOCKED requires dual authorization — never the AI."""
import logging

logger = logging.getLogger("pamm.states")

OP_STATES = ["running", "risk_reduced", "new_trades_paused",
             "close_risk_only", "emergency_flatten", "locked"]
RISK_REDUCED_FACTOR = 0.5


def severity(state: str) -> int:
    return OP_STATES.index(state)


def op_state_of(program: dict) -> str:
    return program.get("op_state") or "running"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def _flatten_and_verify(db, program: dict, actor: str,
                              attempt: int = 1) -> bool:
    """Emergency flatten with VERIFICATION (review v53 §4): never infer the
    book is flat because the close command was issued. Returns True only
    when the broker CONFIRMS zero open positions; otherwise records a
    FLATTEN_FAILED critical incident (retried by the auto-sweep)."""
    from modules.pamm.events import emit_event
    pid = program["program_id"]
    remaining, error = None, None
    try:
        from services.broker_gateway.pamm_api import get_adapter
        adapter = await get_adapter(db, program["partner_id"])
        closed = await adapter.close_all_positions(
            program["broker_program_id"])
        # verify against the broker — the command result is NOT proof
        remaining = len(await adapter.get_positions(
            program["broker_program_id"]))
        if remaining == 0:
            await db.pamm_programs.update_one(
                {"program_id": pid}, {"$unset": {"flatten_failed": ""}})
            await emit_event(db, "PositionsFlattened",
                             {"program_id": pid,
                              "closed": int(closed.get("closed", 0)),
                              "verified_flat": True, "actor": actor,
                              "attempt": attempt})
            if attempt > 1:
                await emit_event(db, "FlattenResolved",
                                 {"program_id": pid, "attempt": attempt})
                await db.pamm_notifications.insert_one(
                    {"type": "FlattenResolved", "program_id": pid,
                     "severity": "info", "at": _now(), "seen": False,
                     "summary": f"Flatten RESOLVED on "
                                f"{program.get('name')} "
                                f"(attempt {attempt}) — book confirmed flat"})
            return True
    except Exception as e:
        error = str(e)[:200]
    incident = {"at": _now(), "attempts": attempt,
                "remaining": remaining, "error": error,
                "first_at": (program.get("flatten_failed") or {}).get(
                    "first_at") or _now()}
    await db.pamm_programs.update_one(
        {"program_id": pid}, {"$set": {"flatten_failed": incident}})
    await emit_event(db, "FlattenFailed",
                     {"program_id": pid, "attempt": attempt,
                      "remaining": remaining, "error": error})
    if attempt == 1 or attempt % 5 == 0:  # first + periodic re-escalation
        await db.pamm_notifications.insert_one(
            {"type": "FlattenFailed", "program_id": pid,
             "severity": "critical", "at": _now(), "seen": False,
             "summary": f"CRITICAL: emergency flatten UNVERIFIED on "
                        f"{program.get('name')} — "
                        f"{remaining if remaining is not None else '?'} "
                        f"position(s) may remain (attempt {attempt}). "
                        f"Human intervention required."})
    logger.critical("PAMM FLATTEN FAILED on %s (attempt %s): remaining=%s "
                    "error=%s", pid, attempt, remaining, error)
    return False


def blocks_new_trades(state: str) -> bool:
    return severity(state) >= severity("new_trades_paused")


async def set_op_state(db, program: dict, target: str, actor: str,
                       reason: str = "", source: str = "human",
                       allow_deescalate: bool = False) -> dict:
    """The ONLY mutator of op_state — also syncs broker trading state and
    the legacy emergency_stop flag."""
    if target not in OP_STATES:
        raise ValueError(f"unknown op state: {target}")
    current = op_state_of(program)
    pid = program["program_id"]
    if target == current:
        return {"program_id": pid, "op_state": current, "changed": False}
    ti, ci = severity(target), severity(current)
    if ti < ci:  # de-escalation — humans only, LOCKED needs dual auth
        if source not in ("human", "dual_auth"):
            raise PermissionError("automation cannot de-escalate op state")
        if current == "locked" and source != "dual_auth":
            raise PermissionError(
                "locked — unlock requires dual authorization")
        if not allow_deescalate:
            raise PermissionError("de-escalation requires confirmation")

    from services.broker_gateway.manager_api import (pause_program,
                                                     resume_program)
    sets = {"op_state": target}
    if ti >= severity("new_trades_paused") and ci < severity(
            "new_trades_paused"):
        try:
            await pause_program(db, program, actor,
                                reason=f"op_state:{target}")
        except Exception as e:  # local state is authoritative for the gate
            logger.error("broker pause failed on %s during op-state %s: %s",
                         pid, target, e)
            sets["trading"] = "paused"
    if target == "emergency_flatten":
        await _flatten_and_verify(db, program, actor)
    if ti >= severity("emergency_flatten"):
        sets["emergency_stop"] = True
    elif program.get("emergency_stop"):
        sets["emergency_stop"] = False
    if ti <= severity("risk_reduced") and ci >= severity(
            "new_trades_paused"):
        await resume_program(db, program, actor)

    await db.pamm_programs.update_one({"program_id": pid}, {"$set": sets})
    from modules.pamm.events import emit_event
    await emit_event(db, "OpStateChanged",
                     {"program_id": pid, "from": current, "to": target,
                      "actor": actor, "source": source,
                      "reason": reason[:200]})
    logger.warning("PAMM op-state %s: %s → %s (%s by %s)",
                   pid, current, target, source, actor)
    return {"program_id": pid, "op_state": target, "changed": True,
            "from": current}
