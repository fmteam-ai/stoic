"""Position Truth — strategy ownership (v62.3). Every PAMM position must
be attributable to PAMM → assignment → strategy/version → intent. Trades
that cannot be attributed are FLAGGED, never guessed."""


async def strategy_ownership(db, program: dict) -> dict:
    from modules.pamm.strategy_assignment import (get_assignment,
                                                  program_account)
    program_id = str(program.get("program_id") or program.get("_id"))
    cur = await get_assignment(db, program_id)
    a = cur.get("assignment")
    acc = await program_account(db, program)
    q = {"status": {"$in": ["open", "pending"]}}
    if acc:
        q["account_id"] = str(acc["_id"])
    else:
        q["program_id"] = program_id
    counts = {"OWNED": 0, "MANUAL_OVERRIDE": 0, "LEGACY_UNTAGGED": 0,
              "PAMM_OWNER_UNKNOWN": 0,
              "PAMM_OWNER_MISMATCH": 0, "STRATEGY_OWNER_UNKNOWN": 0,
              "STRATEGY_VERSION_MISMATCH": 0}
    flagged = []
    async for t in db.trades.find(q, {"pamm_program_id": 1,
                                      "pamm_assignment_id": 1,
                                      "pamm_strategy_id": 1,
                                      "pamm_strategy_version": 1,
                                      "pamm_manual_override": 1,
                                      "execution_intent_id": 1,
                                      "symbol": 1}).limit(500):
        cls = _classify(t, program_id, a)
        counts[cls] += 1
        if cls not in ("OWNED", "LEGACY_UNTAGGED", "MANUAL_OVERRIDE"):
            flagged.append({"trade_id": str(t["_id"]),
                            "symbol": t.get("symbol"),
                            "classification": cls,
                            "pamm_strategy_id": t.get("pamm_strategy_id"),
                            "pamm_strategy_version":
                                t.get("pamm_strategy_version"),
                            "execution_intent_id":
                                t.get("execution_intent_id")})
    healthy = not flagged
    return {"program_id": program_id, "mode": cur.get("mode"),
            "assignment": {"strategy_id": a["strategy_id"],
                           "strategy_version": a["strategy_version"]}
            if a else None,
            "counts": counts, "flagged": flagged, "healthy": healthy,
            "note": "unattributable positions are FLAGGED, never guessed "
                    "— critical before MULTI-strategy"}


def _classify(trade: dict, program_id: str, assignment: dict | None) -> str:
    """Pure ownership classification for one open trade."""
    if trade.get("pamm_manual_override"):
        # explicit override path — owned by the PROGRAM, not any strategy
        return ("PAMM_OWNER_MISMATCH"
                if trade.get("pamm_program_id")
                and trade["pamm_program_id"] != program_id
                else "MANUAL_OVERRIDE")
    t_pid = trade.get("pamm_program_id")
    t_sid = trade.get("pamm_strategy_id")
    if not t_pid and not t_sid:
        # untagged trade: fine on a LEGACY program, unknown owner once a
        # strategy assignment is ACTIVE
        if assignment and assignment.get("status") == "ACTIVE":
            return "PAMM_OWNER_UNKNOWN"
        return "LEGACY_UNTAGGED"
    if t_pid and t_pid != program_id:
        return "PAMM_OWNER_MISMATCH"
    if not assignment:
        return "STRATEGY_OWNER_UNKNOWN"
    if t_sid and t_sid != assignment.get("strategy_id"):
        return "STRATEGY_OWNER_UNKNOWN"
    if (trade.get("pamm_strategy_version")
            and trade["pamm_strategy_version"]
            != assignment.get("strategy_version")):
        return "STRATEGY_VERSION_MISMATCH"
    return "OWNED"
