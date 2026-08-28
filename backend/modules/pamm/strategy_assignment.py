"""PAMM Strategy Assignment (v62.1) — the controlled layer that lets a
PAMM program run a registered strategy profile:

  PAMM chooses the strategy profile. The strategy generates decisions.
  PAMM risk governs capital. Execution Authority remains the only path
  to MT5.

Rules enforced here:
  · SINGLE mode only (multi/dynamic behind feature flags, later phases)
  · assignments pin EXACT version + config hash (never 'latest')
  · programs without an assignment run in LEGACY mode — zero behavior
    change until an admin explicitly migrates them
  · strategy changes require a FLAT program (DRAIN/FLATTEN workflows
    arrive in v62.2)
  · every mutation emits an immutable audit record"""
import os
from datetime import datetime, timezone
from uuid import uuid4

from strategies.registry import get_strategy, strategy_hash
from strategies.versions import version_pin_valid


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def feature_flags() -> dict:
    e = os.environ.get
    return {
        "PAMM_STRATEGY_ASSIGNMENT":
            e("PAMM_STRATEGY_ASSIGNMENT", "true").lower() == "true",
        "PAMM_MULTI_STRATEGY":
            e("PAMM_MULTI_STRATEGY", "false").lower() == "true",
        "PAMM_DYNAMIC_AI":
            e("PAMM_DYNAMIC_AI", "false").lower() == "true",
        "PAMM_NITRO_LIVE":
            e("PAMM_NITRO_LIVE", "false").lower() == "true",
        "PAMM_REQUIRE_CERTIFICATION":
            e("PAMM_REQUIRE_CERTIFICATION", "true").lower() == "true",
    }


async def program_account(db, program: dict) -> dict | None:
    acc_id = program.get("master_account_id")
    if not acc_id:
        return None
    from bson import ObjectId
    try:
        return await db.accounts.find_one({"_id": ObjectId(str(acc_id))})
    except Exception:  # noqa: BLE001 — non-ObjectId ids
        return await db.accounts.find_one({"_id": acc_id})


_indexes_done = False


async def ensure_indexes(db) -> None:
    global _indexes_done
    if _indexes_done:
        return
    c = db.pamm_strategy_assignments
    await c.create_index([("pamm_program_id", 1)], unique=True,
                         partialFilterExpression={"status": "ACTIVE"},
                         name="uniq_active_per_program")
    await c.create_index([("pamm_program_id", 1), ("strategy_id", 1)])
    await c.create_index([("strategy_id", 1), ("strategy_version", 1)])
    await c.create_index([("pamm_program_id", 1),
                          ("certification_status", 1)])
    _indexes_done = True


async def _audit(db, etype: str, program_id: str, actor: str,
                 detail: dict) -> None:
    from modules.pamm.events import emit_event
    await emit_event(db, etype, {"program_id": program_id,
                                 "actor": actor, **detail})


async def get_assignment(db, program_id: str) -> dict:
    a = await db.pamm_strategy_assignments.find_one(
        {"pamm_program_id": program_id,
         "status": {"$in": ["ASSIGNED", "ACTIVE", "SUSPENDED"]}},
        {"_id": 0}, sort=[("created_at", -1)])
    if not a:
        return {"mode": "LEGACY", "assignment": None,
                "note": "no strategy assignment — program behaves exactly "
                        "as before (migration is explicit, never forced)"}
    return {"mode": a["mode"], "assignment": a}


async def assign(db, program: dict, payload: dict, actor: str) -> dict:
    await ensure_indexes(db)
    flags = feature_flags()
    if not flags["PAMM_STRATEGY_ASSIGNMENT"]:
        return {"error": "feature_disabled",
                "detail": "PAMM_STRATEGY_ASSIGNMENT is off"}
    mode = str(payload.get("mode") or "SINGLE").upper()
    if mode != "SINGLE":
        if mode == "MULTI" and not flags["PAMM_MULTI_STRATEGY"]:
            return {"error": "mode_not_enabled", "detail": "MULTI is "
                    "feature-flagged off (phase 2)"}
        if mode == "DYNAMIC_AI" and not flags["PAMM_DYNAMIC_AI"]:
            return {"error": "mode_not_enabled", "detail": "DYNAMIC_AI is "
                    "feature-flagged off (phase 3)"}
        return {"error": "mode_not_enabled",
                "detail": "only SINGLE is live in v62.1"}
    d = get_strategy(str(payload.get("strategy_id") or ""))
    if not d or not d.enabled or not d.pamm_eligible:
        return {"error": "strategy_not_eligible"}
    from modules.pamm.risk_profiles import get_profile
    rp_id = str(payload.get("risk_profile_id") or "controlled")
    if not await get_profile(db, rp_id):
        return {"error": "unknown_risk_profile"}
    program_id = str(program.get("program_id") or program.get("_id"))
    existing = await db.pamm_strategy_assignments.find_one(
        {"pamm_program_id": program_id,
         "status": {"$in": ["ASSIGNED", "ACTIVE"]}})
    if existing:
        return {"error": "assignment_exists",
                "assignment_id": existing["assignment_id"],
                "detail": "SINGLE mode allows one assignment — use "
                          "/strategy/change"}
    doc = {"assignment_id": "psa_" + uuid4().hex[:12],
           "pamm_program_id": program_id,
           "strategy_id": d.strategy_id,
           "strategy_version": d.version,
           "strategy_hash": strategy_hash(d),
           "mode": mode, "enabled": True,
           "risk_profile_id": rp_id,
           "min_weight": float(payload.get("min_weight") or 0.0),
           "target_weight": float(payload.get("target_weight") or 1.0),
           "max_weight": float(payload.get("max_weight") or 1.0),
           "certification_status": "UNCERTIFIED",
           "status": "ASSIGNED",
           "created_by": actor, "created_at": _now(),
           "approved_by": None, "approved_at": None,
           "activated_at": None, "suspended_at": None,
           "last_validation": None, "version": 1}
    await db.pamm_strategy_assignments.insert_one(dict(doc))
    doc.pop("_id", None)
    await _audit(db, "PAMM_STRATEGY_ASSIGNED", program_id, actor,
                 {"strategy_id": d.strategy_id,
                  "strategy_version": d.version,
                  "strategy_hash": doc["strategy_hash"],
                  "risk_profile_id": rp_id})
    return doc


async def patch(db, program: dict, payload: dict, actor: str) -> dict:
    program_id = str(program.get("program_id") or program.get("_id"))
    a = await db.pamm_strategy_assignments.find_one(
        {"pamm_program_id": program_id,
         "status": {"$in": ["ASSIGNED", "ACTIVE", "SUSPENDED"]}},
        sort=[("created_at", -1)])
    if not a:
        return {"error": "no_assignment"}
    updates: dict = {}
    if "risk_profile_id" in payload:
        from modules.pamm.risk_profiles import get_profile
        rp = str(payload["risk_profile_id"])
        if not await get_profile(db, rp):
            return {"error": "unknown_risk_profile"}
        updates["risk_profile_id"] = rp
    for k in ("min_weight", "target_weight", "max_weight"):
        if k in payload:
            updates[k] = float(payload[k])
    if "enabled" in payload:
        updates["enabled"] = bool(payload["enabled"])
    if not updates:
        return {"error": "nothing_to_update"}
    updates["version"] = int(a.get("version") or 1) + 1
    await db.pamm_strategy_assignments.update_one(
        {"assignment_id": a["assignment_id"]}, {"$set": updates})
    await _audit(db, "PAMM_STRATEGY_ASSIGNED", program_id, actor,
                 {"assignment_id": a["assignment_id"],
                  "patched": sorted(updates), "action": "patch"})
    return {**{k: v for k, v in a.items() if k != "_id"}, **updates}


async def validate(db, program: dict, actor: str) -> dict:
    program_id = str(program.get("program_id") or program.get("_id"))
    a = await db.pamm_strategy_assignments.find_one(
        {"pamm_program_id": program_id,
         "status": {"$in": ["ASSIGNED", "ACTIVE", "SUSPENDED"]}},
        sort=[("created_at", -1)])
    if not a:
        return {"error": "no_assignment"}
    d = get_strategy(a["strategy_id"])
    pin = version_pin_valid(a["strategy_id"], a["strategy_version"],
                            a["strategy_hash"])
    from modules.pamm.risk_profiles import get_profile
    checks = [
        {"key": "strategy_registered", "ok": bool(d and d.enabled)},
        {"key": "pamm_eligible", "ok": bool(d and d.pamm_eligible)},
        {"key": "version_pin", "ok": pin["ok"], "detail": pin},
        {"key": "risk_profile", "ok": bool(
            await get_profile(db, a["risk_profile_id"]))},
    ]
    nitro = None
    if d and d.requires_latency_certification:
        from strategies.nitro.eligibility import eligibility
        manager = str(program.get("manager_user_id")
                      or program.get("created_by") or actor)
        nitro = await eligibility(db, manager)
        checks.append({"key": "execution_eligibility",
                       "ok": nitro["status"] != "NITRO_PAUSED",
                       "detail": {"score": nitro["score"],
                                  "status": nitro["status"]}})
    passed = all(c["ok"] for c in checks)
    result = {"passed": passed, "checks": checks, "at": _now(),
              "nitro_eligibility": nitro}
    await db.pamm_strategy_assignments.update_one(
        {"assignment_id": a["assignment_id"]},
        {"$set": {"last_validation": result}})
    await _audit(db, "PAMM_STRATEGY_VALIDATED", program_id, actor,
                 {"assignment_id": a["assignment_id"], "passed": passed})
    return {"assignment_id": a["assignment_id"], **result}


async def activate(db, program: dict, actor: str) -> dict:
    program_id = str(program.get("program_id") or program.get("_id"))
    a = await db.pamm_strategy_assignments.find_one(
        {"pamm_program_id": program_id,
         "status": {"$in": ["ASSIGNED", "SUSPENDED"]}},
        sort=[("created_at", -1)])
    if not a:
        return {"error": "no_activatable_assignment"}
    lv = a.get("last_validation") or {}
    if not lv.get("passed"):
        return {"error": "validation_required",
                "detail": "run /strategy/validate and pass first"}
    pin = version_pin_valid(a["strategy_id"], a["strategy_version"],
                            a["strategy_hash"])
    if not pin["ok"]:
        return {"error": "version_pin_invalid", "detail": pin}
    if (a["strategy_id"] == "nitro_scalper"
            and not feature_flags()["PAMM_NITRO_LIVE"]):
        return {"error": "nitro_live_disabled",
                "detail": "PAMM_NITRO_LIVE feature flag is off — Nitro "
                          "can be assigned and validated but not "
                          "activated for live capital"}
    if feature_flags()["PAMM_REQUIRE_CERTIFICATION"]:
        acc = await program_account(db, program)
        if acc:
            from broker_env import broker_environment
            if (broker_environment(acc) == "LIVE"
                    and a.get("certification_status") != "CERTIFIED"):
                return {"error": "certification_required",
                        "detail": "LIVE broker environment requires a "
                                  "CERTIFIED PAMM × strategy combo — "
                                  "complete the replay → shadow → demo → "
                                  "canary certification campaign first"}
    await db.pamm_strategy_assignments.update_one(
        {"assignment_id": a["assignment_id"]},
        {"$set": {"status": "ACTIVE", "activated_at": _now(),
                  "approved_by": actor, "approved_at": _now(),
                  "suspended_at": None}})
    await _audit(db, "PAMM_STRATEGY_ACTIVATED", program_id, actor,
                 {"assignment_id": a["assignment_id"],
                  "strategy_id": a["strategy_id"],
                  "strategy_version": a["strategy_version"]})
    return {"assignment_id": a["assignment_id"], "status": "ACTIVE"}


async def suspend(db, program: dict, actor: str, reason: str) -> dict:
    program_id = str(program.get("program_id") or program.get("_id"))
    a = await db.pamm_strategy_assignments.find_one(
        {"pamm_program_id": program_id, "status": "ACTIVE"})
    if not a:
        return {"error": "no_active_assignment"}
    await db.pamm_strategy_assignments.update_one(
        {"assignment_id": a["assignment_id"]},
        {"$set": {"status": "SUSPENDED", "suspended_at": _now(),
                  "suspend_reason": reason}})
    await _audit(db, "PAMM_STRATEGY_SUSPENDED", program_id, actor,
                 {"assignment_id": a["assignment_id"], "reason": reason})
    return {"assignment_id": a["assignment_id"], "status": "SUSPENDED"}


async def _open_positions(db, program: dict) -> int:
    acc_id = program.get("master_account_id")
    q = {"status": "open"}
    if acc_id:
        q["account_id"] = str(acc_id)
    else:
        q["program_id"] = str(program.get("program_id")
                              or program.get("_id"))
    return await db.trades.count_documents(q)


async def change(db, program: dict, payload: dict, actor: str) -> dict:
    """Strategy change on a live PAMM — only allowed when the program is
    FLAT. Positions owned by the old strategy are NEVER inherited by the
    new one (DRAIN/FLATTEN transition plans arrive in v62.2)."""
    program_id = str(program.get("program_id") or program.get("_id"))
    open_n = await _open_positions(db, program)
    if open_n > 0:
        return {"error": "program_not_flat", "open_positions": open_n,
                "detail": "strategy change requires a flat program — "
                          "DRAIN/FLATTEN transition workflows arrive in "
                          "v62.2; close exposure first"}
    old = await db.pamm_strategy_assignments.find_one(
        {"pamm_program_id": program_id,
         "status": {"$in": ["ASSIGNED", "ACTIVE", "SUSPENDED"]}},
        sort=[("created_at", -1)])
    await _audit(db, "PAMM_STRATEGY_CHANGE_REQUESTED", program_id, actor,
                 {"old": {"strategy_id": (old or {}).get("strategy_id"),
                          "strategy_version":
                          (old or {}).get("strategy_version")},
                  "new": {"strategy_id": payload.get("strategy_id")}})
    if old:
        await db.pamm_strategy_assignments.update_one(
            {"assignment_id": old["assignment_id"]},
            {"$set": {"status": "REPLACED", "suspended_at": _now()}})
    out = await assign(db, program, payload, actor)
    if not out.get("error"):
        await _audit(db, "PAMM_STRATEGY_CHANGED", program_id, actor,
                     {"old": (old or {}).get("strategy_id"),
                      "new": out["strategy_id"],
                      "new_version": out["strategy_version"],
                      "strategy_hash": out["strategy_hash"]})
    return out
