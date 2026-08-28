"""v62.2 — PAMM × Strategy Certification Campaigns.

A PAMM × strategy combo earns LIVE status ONLY through the evidence
pipeline  REPLAY → SHADOW → DEMO → CANARY → CERTIFIED → LIVE.
Every stage has explicit PURE pass criteria; every checkpoint and
evaluation is a hash-chained evidence record (tamper-evident); stage
advancement is admin-only + step-up and impossible without a PASSING
evaluation of the current stage. Missing evidence FAILS — never assumed."""
from datetime import datetime, timezone
from uuid import uuid4

from soak_campaign import evidence_hash, verify_chain
from strategies.certification import (AUTO_SUSPEND_TRIGGERS, LIFECYCLE,
                                      cert_identity, transition_allowed)
from strategies.versions import version_pin_valid

EVIDENCE_STAGES = ["REPLAY", "SHADOW", "DEMO", "CANARY"]

NEXT_STATE = {"DRAFT": "VALIDATING", "VALIDATING": "REPLAY",
              "REPLAY": "SHADOW", "SHADOW": "DEMO", "DEMO": "CANARY",
              "CANARY": "CERTIFIED", "CERTIFIED": "LIVE"}

CANARY_MAX_CAPITAL_PCT = 5.0
CERT_VALID_DAYS = 30

STAGE_CRITERIA = {
    "REPLAY": {
        "min_decisions": 200, "determinism_required": True,
        "expectancy_lower_r_gt": 0.0, "max_risk_violations": 0},
    "SHADOW": {
        "min_days": 5, "min_decisions": 100, "min_agreement_rate": 0.95,
        "max_risk_violations": 0, "max_orders_placed": 0},
    "DEMO": {
        "environment": "DEMO", "min_days": 10, "min_closed_trades": 50,
        "max_unknown_rate": 0.02, "max_reject_rate": 0.05,
        "max_drawdown_pct": 10.0, "expectancy_r_gt": 0.0},
    "CANARY": {
        "environment": "LIVE", "max_capital_pct": CANARY_MAX_CAPITAL_PCT,
        "min_days": 5, "min_closed_trades": 20, "max_drawdown_pct": 2.0,
        "max_critical_incidents": 0, "execution_health_not": "RED"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ge(v, t):
    return v is not None and float(v) >= float(t)


def _le(v, t):
    return v is not None and float(v) <= float(t)


def _gt(v, t):
    return v is not None and float(v) > float(t)


def _chk(key: str, ok: bool, actual, required) -> dict:
    return {"key": key, "ok": bool(ok), "actual": actual,
            "required": required}


def evaluate_stage(stage: str, m: dict) -> dict:
    """Pure stage-gate evaluation. Unknown/missing metrics FAIL —
    certification runs on evidence, never on assumptions."""
    c = STAGE_CRITERIA.get(stage)
    if not c:
        return {"stage": stage, "passed": False,
                "checks": [_chk("known_stage", False, stage,
                                EVIDENCE_STAGES)]}
    checks = []
    if stage == "REPLAY":
        checks = [
            _chk("decisions_replayed",
                 _ge(m.get("decisions_replayed"), c["min_decisions"]),
                 m.get("decisions_replayed"), f">= {c['min_decisions']}"),
            _chk("determinism", m.get("determinism_ok") is True,
                 m.get("determinism_ok"), "identical decisions on re-run"),
            _chk("expectancy_lower_bound",
                 _gt(m.get("expectancy_lower_r"), c["expectancy_lower_r_gt"]),
                 m.get("expectancy_lower_r"), "> 0 R (bootstrap lower)"),
            _chk("risk_violations",
                 _le(m.get("risk_violations"), c["max_risk_violations"]),
                 m.get("risk_violations"), "0"),
        ]
    elif stage == "SHADOW":
        checks = [
            _chk("days_elapsed", _ge(m.get("days_elapsed"), c["min_days"]),
                 m.get("days_elapsed"), f">= {c['min_days']}"),
            _chk("shadow_decisions",
                 _ge(m.get("shadow_decisions"), c["min_decisions"]),
                 m.get("shadow_decisions"), f">= {c['min_decisions']}"),
            _chk("agreement_rate",
                 _ge(m.get("agreement_rate"), c["min_agreement_rate"]),
                 m.get("agreement_rate"), f">= {c['min_agreement_rate']}"),
            _chk("risk_violations",
                 _le(m.get("risk_violations"), c["max_risk_violations"]),
                 m.get("risk_violations"), "0"),
            _chk("no_orders_placed",
                 _le(m.get("orders_placed"), c["max_orders_placed"]),
                 m.get("orders_placed"), "0 — shadow NEVER trades"),
        ]
    elif stage == "DEMO":
        checks = [
            _chk("environment", m.get("environment") == "DEMO",
                 m.get("environment"), "DEMO"),
            _chk("days_elapsed", _ge(m.get("days_elapsed"), c["min_days"]),
                 m.get("days_elapsed"), f">= {c['min_days']}"),
            _chk("closed_trades",
                 _ge(m.get("closed_trades"), c["min_closed_trades"]),
                 m.get("closed_trades"), f">= {c['min_closed_trades']}"),
            _chk("unknown_rate",
                 _le(m.get("unknown_rate"), c["max_unknown_rate"]),
                 m.get("unknown_rate"), f"<= {c['max_unknown_rate']}"),
            _chk("reject_rate",
                 _le(m.get("reject_rate"), c["max_reject_rate"]),
                 m.get("reject_rate"), f"<= {c['max_reject_rate']}"),
            _chk("max_drawdown",
                 _le(m.get("max_drawdown_pct"), c["max_drawdown_pct"]),
                 m.get("max_drawdown_pct"), f"<= {c['max_drawdown_pct']}%"),
            _chk("expectancy",
                 _gt(m.get("expectancy_r"), c["expectancy_r_gt"]),
                 m.get("expectancy_r"), "> 0 R"),
        ]
    elif stage == "CANARY":
        checks = [
            _chk("environment", m.get("environment") == "LIVE",
                 m.get("environment"), "LIVE"),
            _chk("capital_cap",
                 _le(m.get("canary_capital_pct"), c["max_capital_pct"]),
                 m.get("canary_capital_pct"),
                 f"<= {c['max_capital_pct']}% of program capital"),
            _chk("days_elapsed", _ge(m.get("days_elapsed"), c["min_days"]),
                 m.get("days_elapsed"), f">= {c['min_days']}"),
            _chk("closed_trades",
                 _ge(m.get("closed_trades"), c["min_closed_trades"]),
                 m.get("closed_trades"), f">= {c['min_closed_trades']}"),
            _chk("max_drawdown",
                 _le(m.get("max_drawdown_pct"), c["max_drawdown_pct"]),
                 m.get("max_drawdown_pct"), f"<= {c['max_drawdown_pct']}%"),
            _chk("critical_incidents",
                 _le(m.get("critical_incidents"),
                     c["max_critical_incidents"]),
                 m.get("critical_incidents"), "0"),
            _chk("execution_health",
                 m.get("execution_health") is not None
                 and m.get("execution_health") != c["execution_health_not"],
                 m.get("execution_health"), "not RED"),
        ]
    return {"stage": stage, "passed": all(ch["ok"] for ch in checks),
            "checks": checks, "criteria": c}


# ───────────────────────── persistence layer ──────────────────────────────

_indexes_done = False


async def ensure_indexes(db) -> None:
    global _indexes_done
    if _indexes_done:
        return
    await db.strategy_cert_campaigns.create_index(
        [("pamm_program_id", 1), ("state", 1)])
    await db.strategy_cert_campaigns.create_index([("campaign_id", 1)],
                                                  unique=True)
    await db.strategy_cert_evidence.create_index(
        [("campaign_id", 1), ("seq", 1)], unique=True)
    _indexes_done = True


async def _audit(db, etype: str, program_id: str, actor: str,
                 detail: dict) -> None:
    from modules.pamm.events import emit_event
    await emit_event(db, etype, {"program_id": program_id,
                                 "actor": actor, **detail})


OPEN_STATES = ["DRAFT", "VALIDATING"] + EVIDENCE_STAGES + \
    ["CERTIFIED", "LIVE", "SUSPENDED"]


async def get_campaign(db, program_id: str) -> dict | None:
    return await db.strategy_cert_campaigns.find_one(
        {"pamm_program_id": program_id, "state": {"$in": OPEN_STATES}},
        {"_id": 0}, sort=[("created_at", -1)])


async def _append_evidence(db, campaign_id: str, kind: str, actor: str,
                           payload: dict) -> dict:
    last = await db.strategy_cert_evidence.find_one(
        {"campaign_id": campaign_id}, sort=[("seq", -1)])
    seq = int(last["seq"]) + 1 if last else 1
    prev = last["hash"] if last else "genesis"
    record = {"campaign_id": campaign_id, "seq": seq, "kind": kind,
              "actor": actor, "at": _now(),
              "payload": {k: v for k, v in payload.items() if k != "_id"},
              "prev_hash": prev}
    record["hash"] = evidence_hash(record, prev)
    await db.strategy_cert_evidence.insert_one(dict(record))
    record.pop("_id", None)
    return record


async def evidence(db, campaign_id: str) -> dict:
    records = [r async for r in db.strategy_cert_evidence.find(
        {"campaign_id": campaign_id}, {"_id": 0}).sort("seq", 1).limit(2000)]
    return {"campaign_id": campaign_id, "records": records,
            "count": len(records), "chain_valid": verify_chain(records),
            "note": "hash-chained append-only evidence — any tampering "
                    "breaks chain_valid"}


# ───────────────────────── campaign lifecycle ──────────────────────────────

async def start(db, program: dict, actor: str) -> dict:
    await ensure_indexes(db)
    program_id = str(program.get("program_id") or program.get("_id"))
    from modules.pamm.strategy_assignment import (get_assignment,
                                                  program_account)
    cur = await get_assignment(db, program_id)
    a = cur.get("assignment")
    if not a:
        return {"error": "no_assignment",
                "detail": "assign a strategy before certifying"}
    existing = await get_campaign(db, program_id)
    if existing:
        return {"error": "campaign_exists",
                "campaign_id": existing["campaign_id"],
                "state": existing["state"]}
    acc = await program_account(db, program)
    from broker_env import broker_environment
    env = broker_environment(acc) if acc else "PAPER"
    ident = cert_identity(
        program_id, a["strategy_id"], a["strategy_version"],
        (acc or {}).get("broker_name") or (acc or {}).get("broker"),
        (acc or {}).get("broker_server") or (acc or {}).get("server"),
        a["risk_profile_id"], env)
    doc = {"campaign_id": "scc_" + uuid4().hex[:12],
           "pamm_program_id": program_id,
           "assignment_id": a["assignment_id"],
           "identity": ident,
           "strategy_hash": a["strategy_hash"],
           "state": "DRAFT", "stage_started_at": _now(),
           "stage_history": [], "last_evaluation": None,
           "cert_id": None, "created_by": actor, "created_at": _now()}
    await db.strategy_cert_campaigns.insert_one(dict(doc))
    doc.pop("_id", None)
    await _append_evidence(db, doc["campaign_id"], "campaign_started",
                           actor, {"identity": ident,
                                   "strategy_hash": a["strategy_hash"]})
    await _audit(db, "PAMM_CERT_CAMPAIGN_STARTED", program_id, actor,
                 {"campaign_id": doc["campaign_id"],
                  "identity_hash": ident["identity_hash"]})
    return doc


async def collect_auto_metrics(db, program: dict, campaign: dict) -> dict:
    """Best-effort auto metrics from real collections — anything the
    platform cannot observe stays absent (and therefore FAILS gates
    until an audited checkpoint supplies it)."""
    from modules.pamm.strategy_assignment import program_account
    program_id = str(program.get("program_id") or program.get("_id"))
    since = campaign.get("stage_started_at") or campaign["created_at"]
    try:
        started = datetime.fromisoformat(str(since))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - started).total_seconds() / 86400
    except (TypeError, ValueError):
        days = 0.0
    q = {"opened_at": {"$gte": str(since)}}
    acc = await program_account(db, program)
    if acc:
        q["account_id"] = str(acc["_id"])
    else:
        q["program_id"] = program_id
    m = {"days_elapsed": round(days, 3),
         "closed_trades": await db.trades.count_documents(
             {**q, "status": "closed"}),
         "open_positions": await db.trades.count_documents(
             {**q, "status": "open"})}
    if acc:
        from broker_env import broker_environment
        m["environment"] = broker_environment(acc)
    return m


async def _latest_checkpoint_metrics(db, campaign_id: str,
                                     stage: str) -> dict:
    r = await db.strategy_cert_evidence.find_one(
        {"campaign_id": campaign_id, "kind": "checkpoint",
         "payload.stage": stage}, sort=[("seq", -1)])
    return dict((r or {}).get("payload", {}).get("metrics") or {})


async def checkpoint(db, program: dict, payload: dict, actor: str) -> dict:
    """Admin-recorded, audited, hash-chained stage checkpoint. Checkpoint
    metrics OVERRIDE auto metrics — they are tamper-evident by chain."""
    program_id = str(program.get("program_id") or program.get("_id"))
    camp = await get_campaign(db, program_id)
    if not camp:
        return {"error": "no_campaign"}
    if camp["state"] not in EVIDENCE_STAGES:
        return {"error": "not_in_evidence_stage", "state": camp["state"]}
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        return {"error": "metrics_required",
                "detail": "checkpoint must carry a non-empty metrics dict"}
    rec = await _append_evidence(
        db, camp["campaign_id"], "checkpoint", actor,
        {"stage": camp["state"], "metrics": metrics,
         "note": str(payload.get("note") or "")[:500]})
    await _audit(db, "PAMM_CERT_CHECKPOINT", program_id, actor,
                 {"campaign_id": camp["campaign_id"],
                  "stage": camp["state"], "seq": rec["seq"]})
    return rec


async def evaluate(db, program: dict, actor: str) -> dict:
    program_id = str(program.get("program_id") or program.get("_id"))
    camp = await get_campaign(db, program_id)
    if not camp:
        return {"error": "no_campaign"}
    if camp["state"] not in EVIDENCE_STAGES:
        return {"error": "not_in_evidence_stage", "state": camp["state"]}
    auto = await collect_auto_metrics(db, program, camp)
    manual = await _latest_checkpoint_metrics(db, camp["campaign_id"],
                                              camp["state"])
    metrics = {**auto, **manual}
    result = evaluate_stage(camp["state"], metrics)
    result.update({"at": _now(), "metrics": metrics,
                   "auto_metrics": sorted(auto),
                   "checkpoint_metrics": sorted(manual)})
    await db.strategy_cert_campaigns.update_one(
        {"campaign_id": camp["campaign_id"]},
        {"$set": {"last_evaluation": result}})
    await _append_evidence(db, camp["campaign_id"], "evaluation", actor,
                           {"stage": camp["state"],
                            "passed": result["passed"],
                            "checks": result["checks"]})
    return {"campaign_id": camp["campaign_id"], **result}


async def _advance_gate(db, program: dict, camp: dict) -> dict:
    """Returns {ok} or {ok:False, error, detail}."""
    state = camp["state"]
    program_id = str(program.get("program_id") or program.get("_id"))
    from modules.pamm.strategy_assignment import get_assignment
    a = (await get_assignment(db, program_id)).get("assignment")
    if not a or a["assignment_id"] != camp["assignment_id"]:
        return {"ok": False, "error": "assignment_gone",
                "detail": "the assignment this campaign certifies no "
                          "longer exists — abort the campaign"}
    pin = version_pin_valid(a["strategy_id"], a["strategy_version"],
                            a["strategy_hash"])
    if not pin["ok"]:
        return {"ok": False, "error": "version_pin_invalid", "detail": pin}
    if state == "DRAFT":
        return {"ok": True}
    if state == "VALIDATING":
        lv = a.get("last_validation") or {}
        if not lv.get("passed"):
            return {"ok": False, "error": "validation_required",
                    "detail": "run /strategy/validate and pass first"}
        return {"ok": True}
    if state in EVIDENCE_STAGES:
        ev = camp.get("last_evaluation") or {}
        if ev.get("stage") != state or not ev.get("passed"):
            return {"ok": False, "error": "stage_evaluation_required",
                    "detail": f"{state} evaluation must PASS before "
                              "advancing — run /certification/evaluate"}
        return {"ok": True}
    if state == "CERTIFIED":
        from certification import cert_validity
        cert = await db.certifications.find_one(
            {"cert_id": camp.get("cert_id")})
        if not cert or not cert_validity(cert)["valid"]:
            return {"ok": False, "error": "certification_invalid",
                    "detail": "issued certification expired or revoked"}
        if a.get("status") != "ACTIVE":
            return {"ok": False, "error": "assignment_not_active",
                    "detail": "activate the strategy assignment before "
                              "going LIVE"}
        return {"ok": True}
    return {"ok": False, "error": "terminal_state", "detail": state}


async def advance(db, program: dict, actor: str) -> dict:
    program_id = str(program.get("program_id") or program.get("_id"))
    camp = await get_campaign(db, program_id)
    if not camp:
        return {"error": "no_campaign"}
    target = NEXT_STATE.get(camp["state"])
    if not target or not transition_allowed(camp["state"], target):
        return {"error": "no_next_state", "state": camp["state"]}
    gate = await _advance_gate(db, program, camp)
    if not gate["ok"]:
        return {"error": gate["error"], "detail": gate.get("detail")}
    updates = {"state": target, "stage_started_at": _now()}
    if target == "CERTIFIED":
        from certification import issue
        cert = await issue(db, actor, {
            "kind": "pamm_strategy",
            "scope": camp["identity"]["identity_hash"],
            "passed": True,
            "checks": (camp.get("last_evaluation") or {}).get("checks", []),
            "tier": "CERTIFIED_CAMPAIGN"})
        updates["cert_id"] = cert["cert_id"]
        await db.pamm_strategy_assignments.update_one(
            {"assignment_id": camp["assignment_id"]},
            {"$set": {"certification_status": "CERTIFIED",
                      "cert_id": cert["cert_id"]}})
        await _audit(db, "PAMM_STRATEGY_CERTIFIED", program_id, actor,
                     {"campaign_id": camp["campaign_id"],
                      "cert_id": cert["cert_id"],
                      "identity_hash": camp["identity"]["identity_hash"]})
    await db.strategy_cert_campaigns.update_one(
        {"campaign_id": camp["campaign_id"]},
        {"$set": updates,
         "$push": {"stage_history": {"from": camp["state"], "to": target,
                                     "by": actor, "at": _now()}}})
    await _append_evidence(db, camp["campaign_id"], "stage_advanced", actor,
                           {"from": camp["state"], "to": target})
    await _audit(db, "PAMM_CERT_STAGE_ADVANCED", program_id, actor,
                 {"campaign_id": camp["campaign_id"],
                  "from": camp["state"], "to": target})
    return {"campaign_id": camp["campaign_id"], "state": target,
            "cert_id": updates.get("cert_id") or camp.get("cert_id")}


async def revoke_campaign(db, program: dict, actor: str,
                          reason: str) -> dict:
    program_id = str(program.get("program_id") or program.get("_id"))
    camp = await get_campaign(db, program_id)
    if not camp:
        return {"error": "no_campaign"}
    if camp["state"] in ("CERTIFIED", "LIVE", "SUSPENDED"):
        target = "REVOKED"
    elif transition_allowed(camp["state"], "DRAFT"):
        target = "DRAFT"  # abort mid-pipeline → back to DRAFT
    else:
        return {"error": "cannot_revoke", "state": camp["state"]}
    updates = {"state": target, "stage_started_at": _now(),
               "revoke_reason": reason}
    if target == "REVOKED":
        if camp.get("cert_id"):
            from certification import revoke as revoke_cert
            await revoke_cert(db, camp["cert_id"], actor, reason)
        await db.pamm_strategy_assignments.update_one(
            {"assignment_id": camp["assignment_id"]},
            {"$set": {"certification_status": "REVOKED"}})
    await db.strategy_cert_campaigns.update_one(
        {"campaign_id": camp["campaign_id"]},
        {"$set": updates,
         "$push": {"stage_history": {"from": camp["state"], "to": target,
                                     "by": actor, "at": _now(),
                                     "reason": reason}}})
    await _append_evidence(db, camp["campaign_id"], "revoked", actor,
                           {"from": camp["state"], "to": target,
                            "reason": reason})
    await _audit(db, "PAMM_STRATEGY_REVOKED", program_id, actor,
                 {"campaign_id": camp["campaign_id"], "reason": reason,
                  "outcome": target})
    return {"campaign_id": camp["campaign_id"], "state": target,
            "reason": reason}


async def status(db, program: dict) -> dict:
    program_id = str(program.get("program_id") or program.get("_id"))
    camp = await get_campaign(db, program_id)
    if not camp:
        return {"campaign": None, "lifecycle": LIFECYCLE,
                "stages": EVIDENCE_STAGES, "criteria": STAGE_CRITERIA,
                "auto_suspend_triggers": AUTO_SUSPEND_TRIGGERS,
                "note": "no certification campaign — start one to earn "
                        "LIVE status through evidence"}
    ev_count = await db.strategy_cert_evidence.count_documents(
        {"campaign_id": camp["campaign_id"]})
    preview = None
    if camp["state"] in EVIDENCE_STAGES:
        auto = await collect_auto_metrics(db, program, camp)
        manual = await _latest_checkpoint_metrics(db, camp["campaign_id"],
                                                  camp["state"])
        preview = evaluate_stage(camp["state"], {**auto, **manual})
    return {"campaign": camp, "lifecycle": LIFECYCLE,
            "stages": EVIDENCE_STAGES, "criteria": STAGE_CRITERIA,
            "next_state": NEXT_STATE.get(camp["state"]),
            "evidence_count": ev_count, "evaluation_preview": preview,
            "auto_suspend_triggers": AUTO_SUSPEND_TRIGGERS}
