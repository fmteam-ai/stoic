"""PAMM Strategy Execution Guard (v62.3, hardened v62.4) — the ONE
authoritative gate a PAMM-originated execution must pass before the
Global Trading Authority. Bound inside execution_authority.submit_intent
AND PaperEngine — no alternate PAMM route exists.

Safety closure chain:
  PAMM → Strategy Governance Mode → Active Assignment REQUIRED →
  Explicit Strategy Provenance → Exact Version+Hash →
  Current Certification Identity → Strategy-Specific Eligibility →
  Effective PAMM Risk Envelope → Canary Envelope → Position Truth →
  Global Trading Authority → Execution Authority → ExecutionIntent → MT5

v62.4 invariants:
  · FAIL CLOSED after migration — a governed program with no ACTIVE
    assignment NEVER reverts to LEGACY
  · explicit provenance — a signal without strategy identity is never
    silently attributed to the assigned strategy
  · manual override is a DEDICATED path (MFA at the route, explicit
    origin, never attributed to the strategy)
  · CANARY capital cap enforced INSIDE execution, not just evidence
  · certification identity re-bound to the CURRENT assignment/account/
    broker/risk profile on every execution (drift ⇒ REJECT)"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("pamm.strategy_guard")

CANARY_MAX_CAPITAL_PCT = 5.0


def _reject(reason: str, checks: list, detail=None,
            mode: str = "STRATEGY") -> dict:
    checks.append({"key": reason, "ok": False, "detail": detail})
    return {"authorized": False, "reason": reason, "mode": mode,
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


async def governance_mode(db, program: dict) -> str:
    """STRATEGY once migrated — the flag on the program OR any assignment
    history (even REPLACED/deleted-active) makes governance sticky."""
    if str(program.get("strategy_governance") or "").upper() == "STRATEGY":
        return "STRATEGY"
    pid = str(program.get("program_id") or program.get("_id"))
    n = await db.pamm_strategy_assignments.count_documents(
        {"pamm_program_id": pid}, limit=1)
    return "STRATEGY" if n else "LEGACY"


# ───────────────── pure envelope / canary rules (unit-testable) ────────────

def envelope_violations(envelope: dict, signal: dict,
                        telemetry: dict) -> list:
    """Effective Risk Envelope enforcement — strictest of PAMM program
    limits and strategy risk profile. Every limit with available evidence
    is enforced; missing telemetry never fabricates a violation."""
    v = []
    env, t = envelope or {}, telemetry or {}
    sym = str(signal.get("symbol") or "").upper()
    allowed = env.get("allowed_symbols")
    if allowed is not None and sym and sym not in {
            str(s).upper() for s in allowed}:
        v.append({"reason": "symbol_not_allowed",
                  "detail": {"symbol": sym, "allowed": allowed}})
    mr = env.get("max_risk_per_trade")
    if (signal.get("risk_pct") is not None and mr is not None
            and float(signal["risk_pct"]) > float(mr)):
        v.append({"reason": "risk_cap_exceeded",
                  "detail": {"requested": float(signal["risk_pct"]),
                             "strictest_max": float(mr)}})
    mo = env.get("max_open_positions")
    if (mo is not None and t.get("open_positions") is not None
            and int(t["open_positions"]) >= int(mo)):
        v.append({"reason": "max_open_positions_reached",
                  "detail": {"open": int(t["open_positions"]),
                             "strictest_max": int(mo)}})
    me = env.get("max_symbol_exposure_lots")
    if me is not None and t.get("symbol_open_lots") is not None:
        proposed = float(t["symbol_open_lots"]) + float(
            signal.get("lot_size") or 0)
        if proposed > float(me):
            v.append({"reason": "symbol_exposure_exceeded",
                      "detail": {"symbol": sym, "proposed_lots": proposed,
                                 "strictest_max": float(me)}})
    dl = env.get("max_daily_loss_pct")
    if (dl is not None and t.get("daily_loss_pct") is not None
            and float(t["daily_loss_pct"]) >= float(dl)):
        v.append({"reason": "daily_loss_cap_reached",
                  "detail": {"daily_loss_pct": float(t["daily_loss_pct"]),
                             "strictest_max": float(dl)}})
    cl = env.get("max_consecutive_losses")
    if (cl is not None and t.get("consecutive_losses") is not None
            and int(t["consecutive_losses"]) >= int(cl)):
        v.append({"reason": "max_consecutive_losses_reached",
                  "detail": {"consecutive": int(t["consecutive_losses"]),
                             "strictest_max": int(cl)}})
    return v


def canary_violation(open_risk_pct_sum: float, new_risk_pct,
                     cap: float = CANARY_MAX_CAPITAL_PCT) -> dict | None:
    """CANARY envelope enforced at EXECUTION time. Fail closed: a canary
    trade without declared risk is unmeasurable ⇒ rejected."""
    if new_risk_pct is None:
        return {"reason": "canary_requires_risk_pct",
                "detail": "canary trades must declare risk_pct — the "
                          "capital cap is unmeasurable otherwise"}
    proposed = float(open_risk_pct_sum or 0) + float(new_risk_pct)
    if proposed > cap:
        return {"reason": "canary_cap_exceeded",
                "detail": {"open_risk_pct": float(open_risk_pct_sum or 0),
                           "requested_risk_pct": float(new_risk_pct),
                           "proposed_total": round(proposed, 4),
                           "cap_pct": cap}}
    return None


def current_identity_hash(program_id: str, assignment: dict,
                          account: dict | None) -> str:
    """The certification identity re-derived from CURRENT facts — any
    drift (broker server move, risk profile change, version change) makes
    old certificates/campaigns non-binding."""
    from broker_env import broker_environment
    from strategies.certification import cert_identity
    env = broker_environment(account) if account else "PAPER"
    return cert_identity(
        program_id, assignment["strategy_id"],
        assignment["strategy_version"],
        (account or {}).get("broker_name") or (account or {}).get("broker"),
        (account or {}).get("broker_server")
        or (account or {}).get("server"),
        assignment["risk_profile_id"], env)["identity_hash"]


# ───────────────────────── telemetry collection ────────────────────────────

async def _telemetry(db, acct_id: str, symbol: str,
                     balance: float | None) -> dict:
    t: dict = {}
    open_q = {"account_id": acct_id, "status": {"$in": ["open", "pending"]}}
    t["open_positions"] = await db.trades.count_documents(open_q)
    sym_lots = 0.0
    risk_sum = 0.0
    async for tr in db.trades.find(open_q, {"lot_size": 1, "symbol": 1,
                                            "risk_pct": 1}).limit(500):
        if str(tr.get("symbol") or "").upper() == str(symbol or "").upper():
            sym_lots += float(tr.get("lot_size") or 0)
        risk_sum += float(tr.get("risk_pct") or 0)
    t["symbol_open_lots"] = round(sym_lots, 4)
    t["open_risk_pct_sum"] = round(risk_sum, 4)
    midnight = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    net = 0.0
    losses_today = False
    async for tr in db.trades.find(
            {"account_id": acct_id, "status": "closed",
             "closed_at": {"$gte": midnight}},
            {"pnl": 1}).limit(1000):
        net += float(tr.get("pnl") or 0)
        losses_today = True
    if losses_today and balance:
        t["daily_loss_pct"] = round(max(0.0, -net) / float(balance) * 100, 4)
    streak = 0
    async for tr in db.trades.find(
            {"account_id": acct_id, "status": "closed"},
            {"pnl": 1}).sort("closed_at", -1).limit(20):
        if float(tr.get("pnl") or 0) < 0:
            streak += 1
        else:
            break
    t["consecutive_losses"] = streak
    return t


async def _effective_envelope(db, program: dict,
                              risk_profile_id: str | None) -> dict:
    from modules.pamm.risk import get_limits
    from modules.pamm.risk_profiles import effective_envelope, get_profile
    profile = (await get_profile(db, risk_profile_id)
               if risk_profile_id else None)
    return effective_envelope(profile, get_limits(program))


# ───────────────────────── the authoritative gate ──────────────────────────

async def authorize_pamm_strategy_execution(db, program: dict,
                                            account: dict,
                                            signal: dict) -> dict:
    checks: list = []
    program_id = str(program.get("program_id") or program.get("_id"))
    acct_id = str((account or {}).get("_id") or "")
    # program operating state permits new risk
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
    # STRATEGY GOVERNANCE MODE — sticky once migrated (fail closed)
    gov = await governance_mode(db, program)
    from modules.pamm.strategy_assignment import get_assignment
    cur = await get_assignment(db, program_id)
    a = cur.get("assignment")
    if gov == "LEGACY":
        _ok(checks, "governance", "LEGACY — never migrated")
        return {"authorized": True, "reason": "legacy_mode",
                "mode": "LEGACY", "checks": checks,
                "context": {"pamm_program_id": program_id}}
    _ok(checks, "governance", "STRATEGY")
    # DEDICATED MANUAL OVERRIDE PATH — MFA + explicit origin enforced at
    # the route; never attributed to the strategy; envelope still applies
    if signal.get("pamm_manual_override") is True:
        env = await _effective_envelope(
            db, program, (a or {}).get("risk_profile_id"))
        t = await _telemetry(db, acct_id, signal.get("symbol"),
                             (account or {}).get("balance")
                             or (account or {}).get("equity"))
        vio = envelope_violations(env, signal, t)
        if vio:
            return _reject(vio[0]["reason"], checks, vio[0]["detail"],
                           mode="MANUAL_OVERRIDE")
        _ok(checks, "manual_override",
            "authorized — NOT attributed to any strategy")
        return {"authorized": True, "reason": "manual_override",
                "mode": "MANUAL_OVERRIDE", "checks": checks,
                "context": {"pamm_program_id": program_id,
                            "manual_override": True,
                            "effective_limits": env}}
    # ACTIVE ASSIGNMENT REQUIRED — a migrated program with a missing or
    # inactive assignment FAILS CLOSED, never reverts to LEGACY
    if not a:
        return _reject("governed_program_requires_assignment", checks,
                       "strategy governance is sticky — assignment "
                       "missing/deleted ⇒ REJECT (use the manual override "
                       "path for emergency management)")
    if a.get("status") != "ACTIVE":
        return _reject("assignment_not_active", checks, a.get("status"))
    if not a.get("enabled", True):
        return _reject("assignment_disabled", checks)
    _ok(checks, "assignment_active", a["assignment_id"])
    # EXPLICIT STRATEGY PROVENANCE — a signal without strategy identity
    # is NEVER silently stamped as the assigned strategy
    sig_sid = str(signal.get("strategy_id") or "").lower()
    if not sig_sid:
        return _reject("strategy_provenance_missing", checks,
                       "governed execution requires the signal to declare "
                       "strategy_id explicitly — silent attribution is "
                       "forbidden")
    if sig_sid != a["strategy_id"]:
        return _reject("strategy_mismatch", checks,
                       {"signal": sig_sid, "assignment": a["strategy_id"]})
    _ok(checks, "strategy_provenance", sig_sid)
    # EXACT VERSION + HASH against the registry
    from strategies.versions import version_pin_valid
    pin = version_pin_valid(a["strategy_id"], a["strategy_version"],
                            a["strategy_hash"])
    if not pin["ok"]:
        return _reject("version_pin_invalid", checks, pin)
    _ok(checks, "version_pin", a["strategy_version"])
    from strategies.registry import get_strategy
    d = get_strategy(a["strategy_id"])
    if not d or not d.enabled:
        return _reject("strategy_not_registered", checks, a["strategy_id"])
    if not d.pamm_eligible:
        return _reject("strategy_not_pamm_eligible", checks)
    _ok(checks, "strategy_registered", d.strategy_id)
    # CURRENT CERTIFICATION IDENTITY — cert/campaign must bind to the
    # identity hash re-derived from CURRENT facts (drift ⇒ REJECT)
    from broker_env import broker_environment
    env_name = broker_environment(account) if account else "PAPER"
    from modules.pamm.strategy_assignment import feature_flags
    canary_mode = False
    if feature_flags()["PAMM_REQUIRE_CERTIFICATION"] and env_name == "LIVE":
        now_hash = current_identity_hash(program_id, a, account)
        from certification import cert_validity
        cert_ok = False
        if a.get("certification_status") == "CERTIFIED" and a.get("cert_id"):
            cert = await db.certifications.find_one(
                {"cert_id": a["cert_id"]})
            if cert and cert_validity(cert)["valid"]:
                if cert.get("subject") != now_hash:
                    return _reject("certification_identity_drift", checks,
                                   {"certified": cert.get("subject"),
                                    "current": now_hash})
                cert_ok = True
                _ok(checks, "certification", a["cert_id"])
        if not cert_ok:
            from strategies.certification_campaign import get_campaign
            camp = await get_campaign(db, program_id)
            if camp and camp.get("state") == "CANARY":
                camp_hash = (camp.get("identity") or {}).get(
                    "identity_hash")
                if camp_hash != now_hash:
                    return _reject("canary_identity_drift", checks,
                                   {"campaign": camp_hash,
                                    "current": now_hash})
                canary_mode = True
                cert_ok = True
                _ok(checks, "certification", "CANARY trial")
        if not cert_ok:
            return _reject("certification_invalid", checks,
                           a.get("certification_status"))
        if d.requires_broker_certification:
            sys_ok = False
            async for c in db.certifications.find(
                    {"kind": "system", "subject": acct_id,
                     "revoked": False}).sort("issued_at", -1).limit(3):
                if cert_validity(c)["valid"]:
                    sys_ok = True
                    break
            if not sys_ok:
                return _reject("broker_certification_invalid", checks,
                               acct_id)
            _ok(checks, "broker_certification", acct_id)
    else:
        _ok(checks, "certification",
            f"not required ({env_name}, flag "
            f"{feature_flags()['PAMM_REQUIRE_CERTIFICATION']})")
    # EFFECTIVE PAMM RISK ENVELOPE — strictest of program limits and the
    # strategy risk profile, ALL limits enforced (never averaged)
    from modules.pamm.risk_profiles import get_profile
    if not await get_profile(db, a["risk_profile_id"]):
        return _reject("risk_profile_invalid", checks,
                       a["risk_profile_id"])
    envelope = await _effective_envelope(db, program, a["risk_profile_id"])
    t = await _telemetry(db, acct_id, signal.get("symbol"),
                         (account or {}).get("balance")
                         or (account or {}).get("equity"))
    vio = envelope_violations(envelope, signal, t)
    if vio:
        return _reject(vio[0]["reason"], checks, vio[0]["detail"])
    _ok(checks, "risk_envelope", envelope)
    # CANARY ENVELOPE — capital cap enforced at EXECUTION time
    if canary_mode:
        cv = canary_violation(t.get("open_risk_pct_sum", 0.0),
                              signal.get("risk_pct"))
        if cv:
            return _reject(cv["reason"], checks, cv["detail"])
        _ok(checks, "canary_envelope",
            {"open_risk_pct": t.get("open_risk_pct_sum"),
             "cap_pct": CANARY_MAX_CAPITAL_PCT})
    # STRATEGY-SPECIFIC EXECUTION ELIGIBILITY (never "Nitro for all")
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
    # Global Trading Authority runs NEXT in submit_intent (unchanged)
    context = {"pamm_program_id": program_id,
               "assignment_id": a["assignment_id"],
               "strategy_id": a["strategy_id"],
               "strategy_version": a["strategy_version"],
               "strategy_hash": a["strategy_hash"],
               "risk_profile_id": a["risk_profile_id"],
               "certification_id": a.get("cert_id") or "",
               "canary_mode": canary_mode,
               "effective_limits": envelope}
    return {"authorized": True, "reason": "authorized", "mode": "STRATEGY",
            "checks": checks, "context": context}
