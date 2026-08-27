"""Position Truth & Reconciliation (v55 §3) — STOIC continuously compares
its EXPECTED open positions against what the broker ACTUALLY holds. Any
divergence (POSITION_DRIFT) freezes new exposure: op-state escalates to
NEW_TRADES_PAUSED, an incident opens, alerts fire. Only a human may
acknowledge the drift (adopt broker truth) and separately resume —
automation NEVER de-escalates, drift NEVER increases trading authority."""
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("pamm.position_truth")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(positions: list) -> dict:
    out = {}
    for p in positions or []:
        pid = str(p.get("position_id") or p.get("ticket")
                  or p.get("id") or "")
        if pid:
            side = str(p.get("side") or p.get("type") or "BUY").upper()
            out[pid] = {"volume": float(p.get("volume")
                                        or p.get("lots") or 0),
                        "symbol": str(p.get("symbol") or "?"),
                        "side": "SELL" if side.startswith("S") else "BUY"}
    return out


def net_exposure(pos_map: dict) -> dict:
    """Physical view for netting accounts: signed net volume per symbol
    (1 signal != 1 broker position — v56 §5)."""
    net = {}
    for p in pos_map.values():
        signed = p["volume"] * (-1 if p["side"] == "SELL" else 1)
        net[p["symbol"]] = round(net.get(p["symbol"], 0.0) + signed, 4)
    return net


async def check_position_truth(db, program: dict,
                               actor: str = "auto-sweep") -> dict:
    from services.broker_gateway.pamm_api import get_adapter
    pid = program["program_id"]
    adapter = await get_adapter(db, program["partner_id"])
    actual = _norm(await adapter.get_positions(
        program["broker_program_id"]))
    expected_doc = await db.pamm_expected_positions.find_one(
        {"program_id": pid}, {"_id": 0})
    tol = float(program.get("drift_tolerance") or 0.0)
    mode = str(program.get("position_mode") or "hedging")
    at = _now()
    base = {"check_id": f"ptc_{uuid.uuid4().hex[:10]}", "program_id": pid,
            "at": at, "tolerance": tol, "mode": mode,
            "broker_count": len(actual)}
    a_net = net_exposure(actual)
    if expected_doc is None:  # first sight → adopt broker truth as baseline
        await _store_expected(db, pid, actual, "baseline")
        result = {**base, "status": "baseline",
                  "expected_count": len(actual), "missing": [],
                  "unexpected": [], "mismatched": [], "classification": [],
                  "expected_net": a_net, "broker_net": a_net}
    else:
        expected = {p["position_id"]: {
            "volume": float(p.get("volume") or 0),
            "symbol": str(p.get("symbol") or "?"),
            "side": str(p.get("side") or "BUY")}
            for p in expected_doc.get("positions") or []}
        e_net = net_exposure(expected)
        if mode == "netting":
            # netting accounts: compare PHYSICAL net exposure per symbol
            symbols = sorted(set(e_net) | set(a_net))
            mismatched = [{"symbol": s, "expected_net": e_net.get(s, 0.0),
                           "actual_net": a_net.get(s, 0.0)}
                          for s in symbols
                          if abs(e_net.get(s, 0.0) - a_net.get(s, 0.0))
                          > tol]
            missing, unexpected = [], []
            classification = (["NET_EXPOSURE_MISMATCH"] if mismatched
                              else [])
        else:  # hedging: per-position identity comparison
            missing = sorted(set(expected) - set(actual))
            unexpected = sorted(set(actual) - set(expected))
            mismatched = [{"position_id": k,
                           "expected": expected[k]["volume"],
                           "actual": actual[k]["volume"]}
                          for k in sorted(set(expected) & set(actual))
                          if abs(expected[k]["volume"]
                                 - actual[k]["volume"]) > tol]
            classification = [c for c, hit in
                              (("MISSING_AT_BROKER", missing),
                               ("UNEXPECTED_AT_BROKER", unexpected),
                               ("VOLUME_MISMATCH", mismatched)) if hit]
        drift = bool(missing or unexpected or mismatched)
        result = {**base, "status": "drift" if drift else "in_sync",
                  "expected_count": len(expected), "missing": missing,
                  "unexpected": unexpected, "mismatched": mismatched,
                  "classification": classification,
                  "expected_net": e_net, "broker_net": a_net}
    await db.pamm_position_truth.insert_one(dict(result))
    result.pop("_id", None)
    summary = {"status": result["status"], "at": at, "tolerance": tol,
               "mode": mode, "broker_count": result["broker_count"],
               "expected_count": result["expected_count"],
               "missing": len(result["missing"]),
               "unexpected": len(result["unexpected"]),
               "mismatched": len(result["mismatched"]),
               "classification": result["classification"]}
    await db.pamm_programs.update_one(
        {"program_id": pid},
        {"$set": {"position_truth": summary,
                  "position_truth_failures": 0}})
    if result["status"] == "drift":
        await _on_drift(db, program, result, actor)
    else:
        await _resolve_open_drift(db, pid)
    return result


async def _store_expected(db, pid: str, positions: dict,
                          source: str) -> None:
    await db.pamm_expected_positions.update_one(
        {"program_id": pid},
        {"$set": {"positions": [{"position_id": k, "volume": v["volume"],
                                 "symbol": v["symbol"], "side": v["side"]}
                                for k, v in positions.items()],
                  "at": _now(), "source": source}}, upsert=True)


async def _on_drift(db, program: dict, result: dict, actor: str) -> None:
    from modules.pamm.events import emit_event
    from modules.pamm.risk.states import op_state_of, set_op_state, severity
    pid = program["program_id"]
    await emit_event(db, "PositionDrift",
                     {"program_id": pid, "check_id": result["check_id"],
                      "missing": result["missing"],
                      "unexpected": result["unexpected"],
                      "classification": result.get("classification") or [],
                      "mismatched": [m.get("position_id") or m.get("symbol")
                                     for m in result["mismatched"]]},
                     source="position_truth")
    # freeze NEW exposure — escalation only, never towards more authority
    if severity(op_state_of(program)) < severity("new_trades_paused"):
        await set_op_state(
            db, program, "new_trades_paused", actor,
            reason="POSITION_DRIFT — broker book diverged from STOIC "
                   "expected state", source="automation")
    detail = {"missing": len(result["missing"]),
              "unexpected": len(result["unexpected"]),
              "mismatched": len(result["mismatched"])}
    open_inc = await db.pamm_incidents.find_one(
        {"program_id": pid, "type": "position_drift", "status": "open"})
    if open_inc:
        await db.pamm_incidents.update_one(
            {"incident_id": open_inc["incident_id"]},
            {"$set": {"detail": detail,
                      "last_check_id": result["check_id"]}})
        return
    inc = {"incident_id": f"inc_{uuid.uuid4().hex[:10]}",
           "type": "position_drift", "program_id": pid,
           "program_name": program.get("name"), "status": "open",
           "opened_at": _now(), "detail": detail,
           "last_check_id": result["check_id"]}
    await db.pamm_incidents.insert_one(dict(inc))
    await emit_event(db, "BrokerIncidentOpened",
                     {"program_id": pid, "incident_id": inc["incident_id"],
                      "kind": "position_drift"})
    await db.pamm_notifications.insert_one(
        {"type": "PositionDrift", "program_id": pid,
         "severity": "critical", "at": _now(), "seen": False,
         "summary": f"POSITION DRIFT on {program.get('name')}: "
                    f"{detail['missing']} missing / "
                    f"{detail['unexpected']} unexpected / "
                    f"{detail['mismatched']} mismatched — new exposure "
                    f"FROZEN pending human acknowledgement"})
    logger.critical("POSITION DRIFT on %s: %s — new exposure frozen",
                    pid, detail)


async def _resolve_open_drift(db, pid: str) -> None:
    inc = await db.pamm_incidents.find_one_and_update(
        {"program_id": pid, "type": "position_drift", "status": "open"},
        {"$set": {"status": "resolved", "resolved_at": _now()}})
    if inc:
        from modules.pamm.events import emit_event
        await emit_event(db, "BrokerIncidentResolved",
                         {"program_id": pid,
                          "incident_id": inc["incident_id"],
                          "kind": "position_drift"})


async def acknowledge_drift(db, program: dict, actor: str) -> dict:
    """Human adopts broker truth as the new expected state. Trading stays
    frozen — resuming remains a separate step-up-gated human action."""
    from modules.pamm.events import emit_event
    from services.broker_gateway.pamm_api import get_adapter
    pid = program["program_id"]
    adapter = await get_adapter(db, program["partner_id"])
    actual = _norm(await adapter.get_positions(
        program["broker_program_id"]))
    await _store_expected(db, pid, actual, f"acknowledged:{actor}")
    await emit_event(db, "PositionDriftAcknowledged",
                     {"program_id": pid, "actor": actor,
                      "adopted_count": len(actual)},
                     source="position_truth")
    return await check_position_truth(db, program, actor=actor)


async def set_drift_tolerance(db, program: dict, tolerance: float,
                              actor: str) -> dict:
    """Lowering tolerance = safer = single action. RAISING tolerance is
    risk-increasing → dual authorization (kind=drift_tolerance_increase)."""
    tol = float(tolerance)
    if tol < 0:
        raise ValueError("tolerance must be >= 0")
    current = float(program.get("drift_tolerance") or 0.0)
    if tol > current:
        raise PermissionError(
            "raising drift tolerance requires dual authorization "
            "(change request kind=drift_tolerance_increase)")
    await db.pamm_programs.update_one(
        {"program_id": program["program_id"]},
        {"$set": {"drift_tolerance": tol}})
    from modules.pamm.events import emit_event
    await emit_event(db, "DriftToleranceChanged",
                     {"program_id": program["program_id"],
                      "from": current, "to": tol, "actor": actor},
                     source="position_truth")
    return {"program_id": program["program_id"], "drift_tolerance": tol}
