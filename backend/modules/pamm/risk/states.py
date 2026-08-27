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
            inc = await db.pamm_incidents.find_one_and_update(
                {"program_id": pid, "type": "flatten_failed",
                 "status": "open"},
                {"$set": {"status": "resolved", "resolved_at": _now()}})
            if inc:
                await emit_event(db, "BrokerIncidentResolved",
                                 {"program_id": pid,
                                  "incident_id": inc["incident_id"]})
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
    await _escalate_flatten(db, program, incident, attempt)
    logger.critical("PAMM FLATTEN FAILED on %s (attempt %s): remaining=%s "
                    "error=%s", pid, attempt, remaining, error)
    return False


async def _escalate_flatten(db, program: dict, incident: dict,
                            attempt: int) -> None:
    """Escalation ladder (review v54 §2): 1→retry only · 2→critical alert ·
    3→formal broker incident · 5→page operator · >5min→external escalation.
    Unresolved exposure is a first-class incident for managed money."""
    from datetime import datetime, timezone
    from modules.pamm.events import emit_event
    pid = program["program_id"]
    name = program.get("name")

    async def _notify(severity: str, summary: str):
        await db.pamm_notifications.insert_one(
            {"type": "FlattenFailed", "program_id": pid,
             "severity": severity, "at": _now(), "seen": False,
             "summary": summary})

    if attempt == 2:
        await _notify("critical",
                      f"CRITICAL: emergency flatten UNVERIFIED on {name} — "
                      f"{incident.get('remaining') if incident.get('remaining') is not None else '?'} "
                      f"position(s) may remain (attempt 2). "
                      f"Human intervention required.")
    open_inc = await db.pamm_incidents.find_one(
        {"program_id": pid, "type": "flatten_failed", "status": "open"})
    if attempt >= 3:
        if not open_inc:
            open_inc = {"incident_id": f"inc_{_uuid_hex()}",
                        "type": "flatten_failed", "program_id": pid,
                        "program_name": name, "status": "open",
                        "opened_at": _now(), "attempts": attempt,
                        "last_error": incident.get("error"),
                        "external_escalated": False}
            await db.pamm_incidents.insert_one(dict(open_inc))
            await emit_event(db, "BrokerIncidentOpened",
                             {"program_id": pid,
                              "incident_id": open_inc["incident_id"],
                              "kind": "flatten_failed"})
            await _notify("critical",
                          f"BROKER INCIDENT OPENED on {name}: emergency "
                          f"flatten unresolved after {attempt} attempts.")
        else:
            await db.pamm_incidents.update_one(
                {"incident_id": open_inc["incident_id"]},
                {"$set": {"attempts": attempt,
                          "last_error": incident.get("error")}})
    if attempt >= 5 and attempt % 5 == 0:
        await _notify("page",
                      f"PAGE OPERATOR: flatten on {name} still unresolved "
                      f"after {attempt} attempts. Manual broker action "
                      f"required NOW.")
    first_at = incident.get("first_at")
    if first_at and open_inc and not open_inc.get("external_escalated"):
        age_s = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(first_at)).total_seconds()
        if age_s > 300:  # >5 minutes → external escalation
            await db.pamm_incidents.update_one(
                {"incident_id": open_inc["incident_id"]},
                {"$set": {"external_escalated": True,
                          "external_escalated_at": _now()}})
            await emit_event(db, "ExternalEscalation",
                             {"program_id": pid,
                              "incident_id": open_inc["incident_id"],
                              "age_seconds": int(age_s)})
            await _notify("page",
                          f"EXTERNAL ESCALATION on {name}: unresolved "
                          f"exposure for over 5 minutes "
                          f"(incident {open_inc['incident_id']}).")


def _uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex[:10]


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
