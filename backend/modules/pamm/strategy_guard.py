"""PAMM Strategy Execution Guard (v62.3) — the ONE authoritative gate a
PAMM-originated execution must pass before the Global Trading Authority.
Bound inside execution_authority.submit_intent — no alternate PAMM route
to MT5 exists, callers cannot forget to invoke it.

  PAMM signal → resolve assignment → verify strategy/version/hash →
  assignment ACTIVE → certification valid → risk profile (strictest) →
  strategy-specific execution eligibility → Position Truth →
  Global Trading Authority → Execution Authority → MT5

Programs WITHOUT an assignment run in LEGACY mode: program-level safety
(op-state, Position Truth) still applies, strategy checks do not — zero
behavior change until an admin explicitly migrates them."""
import logging

logger = logging.getLogger("pamm.strategy_guard")


def _reject(reason: str, checks: list, detail=None) -> dict:
    checks.append({"key": reason, "ok": False, "detail": detail})
    return {"authorized": False, "reason": reason, "mode": "STRATEGY",
            "checks": checks, "context": None}


def _ok(checks: list, key: str, detail=None) -> None:
    checks.append({"key": key, "ok": True, "detail": detail})


async def resolve_program(db, account_id: str, signal: dict) -> dict | None:
    """A signal is PAMM-originated when it names a program OR its account
    is a PAMM master account."""
    pid = signal.get("program_id") or signal.get("pamm_program_id")
    if pid:
        return await db.pamm_programs.find_one({"program_id": str(pid)})
    if account_id:
        return await db.pamm_programs.find_one(
            {"master_account_id": str(account_id),
             "status": {"$nin": ["archived", "closed"]}})
    return None


async def authorize_pamm_strategy_execution(db, program: dict,
                                            account: dict,
                                            signal: dict) -> dict:
    checks: list = []
    program_id = str(program.get("program_id") or program.get("_id"))
    # 1-2 — program exists + operating state permits new risk
    from modules.pamm.risk import trading_allowed
    allowed, why = await trading_allowed(db, program)
    if not allowed:
        return _reject("program_state_blocks_trading", checks, why)
    _ok(checks, "program_state", why)
    # Position Truth healthy (defence-in-depth; drift also pauses op-state)
    pt = program.get("position_truth") or {}
    if pt.get("status") == "drift":
        return _reject("position_truth_drift", checks,
                       pt.get("classification"))
    _ok(checks, "position_truth", pt.get("status") or "never_checked")
    # 3 — active assignment (none ⇒ LEGACY: unchanged behavior)
    from modules.pamm.strategy_assignment import get_assignment
    cur = await get_assignment(db, program_id)
    a = cur.get("assignment")
    if not a or cur.get("mode") == "LEGACY":
        _ok(checks, "assignment", "LEGACY — no strategy assignment")
        return {"authorized": True, "reason": "legacy_mode",
                "mode": "LEGACY", "checks": checks,
                "context": {"pamm_program_id": program_id}}
    if a.get("status") != "ACTIVE":
        return _reject("assignment_not_active", checks, a.get("status"))
    # 4 — assignment enabled
    if not a.get("enabled", True):
        return _reject("assignment_disabled", checks)
    _ok(checks, "assignment_active", a["assignment_id"])
    # 5 — strategy matches the assignment (when the signal declares one)
    sig_sid = str(signal.get("strategy_id") or "").lower()
    if sig_sid and sig_sid != a["strategy_id"]:
        return _reject("strategy_mismatch", checks,
                       {"signal": sig_sid, "assignment": a["strategy_id"]})
    # 6-7 — exact version + hash pin against the registry
    from strategies.versions import version_pin_valid
    pin = version_pin_valid(a["strategy_id"], a["strategy_version"],
                            a["strategy_hash"])
    if not pin["ok"]:
        return _reject("version_pin_invalid", checks, pin)
    _ok(checks, "version_pin", a["strategy_version"])
    # 8-9 — registered / enabled / PAMM-eligible
    from strategies.registry import get_strategy
    d = get_strategy(a["strategy_id"])
    if not d or not d.enabled:
        return _reject("strategy_not_registered", checks, a["strategy_id"])
    if not d.pamm_eligible:
        return _reject("strategy_not_pamm_eligible", checks)
    _ok(checks, "strategy_registered", d.strategy_id)
    # 10-11 — PAMM×strategy certification + broker/system certification
    from broker_env import broker_environment
    env = broker_environment(account) if account else "PAPER"
    from modules.pamm.strategy_assignment import feature_flags
    if feature_flags()["PAMM_REQUIRE_CERTIFICATION"] and env == "LIVE":
        from certification import cert_validity
        cert_ok = False
        if a.get("certification_status") == "CERTIFIED" and a.get("cert_id"):
            cert = await db.certifications.find_one(
                {"cert_id": a["cert_id"]})
            cert_ok = bool(cert and cert_validity(cert)["valid"])
        if not cert_ok:
            from strategies.certification_campaign import get_campaign
            camp = await get_campaign(db, program_id)
            if camp and camp.get("state") == "CANARY":
                cert_ok = True  # canary trial: capped capital, pre-cert
                _ok(checks, "certification", "CANARY trial")
        if not cert_ok:
            return _reject("certification_invalid", checks,
                           a.get("certification_status"))
        if not checks or checks[-1]["key"] != "certification":
            _ok(checks, "certification", a.get("cert_id"))
        if d.requires_broker_certification:
            acc_id = str((account or {}).get("_id") or "")
            sys_ok = False
            async for c in db.certifications.find(
                    {"kind": "system", "subject": acc_id,
                     "revoked": False}).sort("issued_at", -1).limit(3):
                if cert_validity(c)["valid"]:
                    sys_ok = True
                    break
            if not sys_ok:
                return _reject("broker_certification_invalid", checks,
                               acc_id)
            _ok(checks, "broker_certification", acc_id)
    else:
        _ok(checks, "certification",
            f"not required ({env}, flag "
            f"{feature_flags()['PAMM_REQUIRE_CERTIFICATION']})")
    # 12 — risk profile valid + STRICTEST limits enforced (never averaged)
    from modules.pamm.risk_profiles import get_profile, strictest_limit
    profile = await get_profile(db, a["risk_profile_id"])
    if not profile:
        return _reject("risk_profile_invalid", checks,
                       a["risk_profile_id"])
    max_risk = strictest_limit(profile.get("max_risk_per_trade"))
    sig_risk = signal.get("risk_pct")
    if sig_risk is not None and max_risk is not None \
            and float(sig_risk) > float(max_risk):
        return _reject("risk_cap_exceeded", checks,
                       {"requested": float(sig_risk),
                        "strictest_max": float(max_risk)})
    max_open = strictest_limit(profile.get("max_open_positions"))
    if max_open is not None:
        acc_id = str((account or {}).get("_id") or "")
        open_n = await db.trades.count_documents(
            {"account_id": acc_id, "status": {"$in": ["open", "pending"]}})
        if open_n >= int(max_open):
            return _reject("max_open_positions_reached", checks,
                           {"open": open_n, "strictest_max": int(max_open)})
    _ok(checks, "risk_profile", {"risk_profile_id": a["risk_profile_id"],
                                 "max_risk_per_trade": max_risk,
                                 "max_open_positions": max_open})
    # 13 — strategy-specific execution eligibility (never "Nitro for all")
    from strategies.execution_eligibility import (POLICIES,
                                                  execution_eligibility)
    if POLICIES.get(a["strategy_id"]):
        manager = str(program.get("manager_user_id")
                      or program.get("manager_id")
                      or program.get("created_by") or "")
        elig = await execution_eligibility(db, a["strategy_id"], manager,
                                           account)
        if elig["status"] == "INELIGIBLE":
            return _reject("execution_ineligible", checks,
                           {"policy": elig["policy"],
                            "score": elig["score"],
                            "reason": elig["reason"]})
        _ok(checks, "execution_eligibility",
            {"policy": elig["policy"], "score": elig["score"],
             "status": elig["status"]})
    # 14 — Global Trading Authority runs NEXT in submit_intent (unchanged)
    context = {"pamm_program_id": program_id,
               "assignment_id": a["assignment_id"],
               "strategy_id": a["strategy_id"],
               "strategy_version": a["strategy_version"],
               "strategy_hash": a["strategy_hash"],
               "risk_profile_id": a["risk_profile_id"],
               "certification_id": a.get("cert_id") or "",
               "effective_limits": {"max_risk_per_trade": max_risk,
                                    "max_open_positions": max_open}}
    return {"authorized": True, "reason": "authorized", "mode": "STRATEGY",
            "checks": checks, "context": context}
