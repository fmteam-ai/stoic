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
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("pamm.strategy_guard")

# CANARY cap — what is actually measured is the SUM OF OPEN risk_pct on
# the master account (open-risk cap), NOT deployed capital.
CANARY_MAX_OPEN_RISK_PCT = 5.0

# v62.6 Risk Truth — REQUIRED evidence for LIVE governed executions.
# Missing any REQUIRED item ⇒ RISK_UNKNOWN ⇒ new exposure is blocked.
REQUIRED_TELEMETRY = ("open_positions", "open_risk_pct_sum",
                      "daily_loss_pct", "weekly_loss_pct", "drawdown_pct",
                      "spread_pips")
OPTIONAL_TELEMETRY = ("symbol_open_lots", "consecutive_losses",
                      "factor_lots", "recent_slippage_pips")

# Strategy-specific telemetry freshness (seconds), keyed by the registry
# latency_sensitivity — a Nitro execution demands far fresher evidence.
SPREAD_FRESHNESS_S = {"LOW": 300, "MEDIUM": 300, "MEDIUM_HIGH": 120,
                      "HIGH": 60, "VERY_HIGH": 30, "MAXIMUM": 15}
POSITION_TRUTH_FRESHNESS_S = {"LOW": 900, "MEDIUM": 900,
                              "MEDIUM_HIGH": 600, "HIGH": 600,
                              "VERY_HIGH": 300, "MAXIMUM": 300}


def telemetry_freshness(strategy_id: str | None) -> dict:
    sens = "MEDIUM"
    if strategy_id:
        from strategies.registry import get_strategy
        d = get_strategy(strategy_id)
        if d:
            sens = str((d.characteristics or {}).get("latency_sensitivity")
                       or "MEDIUM").upper()
    return {"latency_sensitivity": sens,
            "spread_s": SPREAD_FRESHNESS_S.get(sens, 300),
            "position_truth_s": POSITION_TRUTH_FRESHNESS_S.get(sens, 900)}


def missing_required_telemetry(telemetry: dict) -> list:
    return [k for k in REQUIRED_TELEMETRY
            if (telemetry or {}).get(k) is None]


def symbol_factors(symbol) -> list:
    """XAUUSD → [XAU, USD]; factor = 3-letter currency/asset bucket."""
    s = str(symbol or "").upper()
    return [c for c in (s[:3], s[3:6]) if len(c) == 3 and c.isalpha()]


def _age_s(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds())
    except (ValueError, TypeError):
        return None


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
    wl = env.get("max_weekly_loss_pct")
    if (wl is not None and t.get("weekly_loss_pct") is not None
            and float(t["weekly_loss_pct"]) >= float(wl)):
        v.append({"reason": "weekly_loss_cap_reached",
                  "detail": {"weekly_loss_pct": float(t["weekly_loss_pct"]),
                             "strictest_max": float(wl)}})
    dd = env.get("max_drawdown_pct")
    if (dd is not None and t.get("drawdown_pct") is not None
            and float(t["drawdown_pct"]) >= float(dd)):
        v.append({"reason": "drawdown_cap_reached",
                  "detail": {"drawdown_pct": float(t["drawdown_pct"]),
                             "strictest_max": float(dd)}})
    sp = env.get("max_spread_pips")
    if (sp is not None and t.get("spread_pips") is not None
            and float(t["spread_pips"]) > float(sp)):
        v.append({"reason": "spread_cap_exceeded",
                  "detail": {"symbol": sym,
                             "spread_pips": float(t["spread_pips"]),
                             "max_spread_pips": float(sp)}})
    sl = env.get("max_slippage_pips")
    if (sl is not None and t.get("recent_slippage_pips") is not None
            and float(t["recent_slippage_pips"]) > float(sl)):
        v.append({"reason": "expected_slippage_exceeded",
                  "detail": {"recent_median_slippage_pips":
                             float(t["recent_slippage_pips"]),
                             "max_slippage_pips": float(sl),
                             "note": "pre-trade block on measured fill "
                                     "evidence; post-trade fills are "
                                     "enforced at the bridge"}})
    fe = env.get("max_factor_exposure_lots")
    if fe is not None and t.get("factor_lots") is not None:
        new_lot = float(signal.get("lot_size") or 0)
        for f in symbol_factors(sym):
            proposed = float((t["factor_lots"] or {}).get(f, 0.0)) + new_lot
            if proposed > float(fe):
                v.append({"reason": "factor_exposure_exceeded",
                          "detail": {"factor": f,
                                     "proposed_lots": round(proposed, 4),
                                     "strictest_max": float(fe)}})
                break
    return v


def canary_violation(open_risk_pct_sum: float, new_risk_pct,
                     cap: float = CANARY_MAX_OPEN_RISK_PCT) -> dict | None:
    """CANARY OPEN-RISK cap enforced at EXECUTION time — the measured
    quantity is the sum of open risk_pct, not deployed capital. Fail
    closed: a canary trade without declared risk is unmeasurable ⇒
    rejected."""
    if new_risk_pct is None:
        return {"reason": "canary_requires_risk_pct",
                "detail": "canary trades must declare risk_pct — the "
                          "open-risk cap is unmeasurable otherwise"}
    proposed = float(open_risk_pct_sum or 0) + float(new_risk_pct)
    if proposed > cap:
        return {"reason": "canary_open_risk_cap_exceeded",
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

async def _consensus_spread(db, symbol: str, exclude_acct_id: str):
    """Cross-account median spread for the symbol from other recently
    heartbeating accounts — an independent plausibility reference against
    a tampered EA self-report. None when fewer than 2 peers exist."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=10)).isoformat()
    q = {"spreads_updated_at": {"$gte": cutoff},
         f"current_spreads.{symbol}": {"$exists": True}}
    try:
        from bson import ObjectId
        q["_id"] = {"$ne": ObjectId(exclude_acct_id)}
    except Exception:
        pass
    vals = []
    async for a in db.accounts.find(
            q, {f"current_spreads.{symbol}": 1}).limit(20):
        try:
            vals.append(float(a["current_spreads"][symbol]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(vals) < 2:
        return None
    vals.sort()
    return vals[len(vals) // 2]


NAV_FRESHNESS_S = 900  # broker-truth NAV older than this is NOT evidence

GUARD_VERSION = "v62.7"


def _execution_policy_version() -> str:
    try:
        from execution_authority import EXECUTION_POLICY_VERSION
        return str(EXECUTION_POLICY_VERSION)
    except Exception:  # noqa: BLE001
        return "unknown"


def _snapshot_hash(snap: dict) -> str:
    """Tamper-evident content hash over the canonical snapshot."""
    import hashlib
    import json
    body = {k: v for k, v in snap.items() if k != "hash"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def is_risk_reducing(signal: dict) -> bool:
    """Fail-safe asymmetry — RISK_UNKNOWN blocks NEW exposure, NEVER a
    safety exit. Close/reduce intents must stay executable."""
    s = signal or {}
    action = str(s.get("action") or s.get("side") or "").upper()
    return bool(s.get("reduce_only") or s.get("close_trade")
                or s.get("pamm_risk_reducing") is True
                or s.get("intent") in ("close", "reduce")
                or action in ("CLOSE", "REDUCE", "FLATTEN"))


async def _telemetry(db, program: dict, account: dict, signal: dict,
                     strategy_id: str | None = None) -> dict:
    """Risk Truth collector — every REQUIRED item resolves to a value or
    stays None (RISK_UNKNOWN evidence gap). A known balance with zero
    closed trades is EVIDENCE of zero loss, never a gap. Any read
    failure degrades THAT item to None (RISK_UNKNOWN on LIVE) — never
    an exception, never an accidental zero."""
    acct_id = str((account or {}).get("_id") or "")
    symbol = str(signal.get("symbol") or "").upper()
    balance = ((account or {}).get("balance")
               or (account or {}).get("equity"))
    t: dict = {"open_positions": None, "open_risk_pct_sum": None,
               "symbol_open_lots": None, "factor_lots": None,
               "daily_loss_pct": None, "weekly_loss_pct": None,
               "drawdown_pct": None, "spread_pips": None,
               "recent_slippage_pips": None, "consecutive_losses": None}
    open_q = {"account_id": acct_id, "status": {"$in": ["open", "pending"]}}
    try:
        t["open_positions"] = await db.trades.count_documents(open_q)
        sym_lots, risk_sum = 0.0, 0.0
        factor_lots: dict = {}
        async for tr in db.trades.find(open_q, {"lot_size": 1, "symbol": 1,
                                                "risk_pct": 1}).limit(500):
            lots = float(tr.get("lot_size") or 0)
            if str(tr.get("symbol") or "").upper() == symbol:
                sym_lots += lots
            for f in symbol_factors(tr.get("symbol")):
                factor_lots[f] = round(factor_lots.get(f, 0.0) + lots, 4)
            risk_sum += float(tr.get("risk_pct") or 0)
        t["symbol_open_lots"] = round(sym_lots, 4)
        t["factor_lots"] = factor_lots
        t["open_risk_pct_sum"] = round(risk_sum, 4)
    except Exception as e:  # noqa: BLE001 — evidence gap, not a crash
        logger.error("risk-truth open-exposure read failed acct=%s: %s",
                     acct_id, e)
    # daily + weekly realized loss — needs a known balance denominator
    if balance:
        try:
            now = datetime.now(timezone.utc)
            day0 = now.strftime("%Y-%m-%dT00:00:00")
            week0 = (now - timedelta(days=now.weekday())).strftime(
                "%Y-%m-%dT00:00:00")
            day_net, week_net = 0.0, 0.0
            async for tr in db.trades.find(
                    {"account_id": acct_id, "status": "closed",
                     "closed_at": {"$gte": week0}},
                    {"pnl": 1, "closed_at": 1}).limit(2000):
                pnl = float(tr.get("pnl") or 0)
                week_net += pnl
                if str(tr.get("closed_at") or "") >= day0:
                    day_net += pnl
            t["daily_loss_pct"] = round(
                max(0.0, -day_net) / float(balance) * 100, 4)
            t["weekly_loss_pct"] = round(
                max(0.0, -week_net) / float(balance) * 100, 4)
        except Exception as e:  # noqa: BLE001
            logger.error("risk-truth loss read failed acct=%s: %s",
                         acct_id, e)
            t["daily_loss_pct"] = None
            t["weekly_loss_pct"] = None
    # drawdown — broker-truth NAV history (peak vs current), and the
    # NAV itself must be FRESH: stale NAV is not evidence
    try:
        pid = str((program or {}).get("program_id")
                  or (program or {}).get("_id") or "")
        last_nav = (program or {}).get("last_nav") or {}
        current_nav, nav_at = last_nav.get("nav"), last_nav.get("at")
        if current_nav is None and pid:
            latest = await db.pamm_nav_snapshots.find_one(
                {"program_id": pid}, {"_id": 0, "nav": 1, "at": 1},
                sort=[("at", -1)])
            if latest:
                current_nav, nav_at = latest["nav"], latest.get("at")
        nav_age = _age_s(nav_at)
        t["nav_age_s"] = round(nav_age, 1) if nav_age is not None else None
        if pid and current_nav is not None:
            if nav_age is None or nav_age > NAV_FRESHNESS_S:
                t["nav_stale"] = {"age_s": t["nav_age_s"],
                                  "max_age_s": NAV_FRESHNESS_S}
            else:
                peak = None
                async for n in db.pamm_nav_snapshots.find(
                        {"program_id": pid},
                        {"_id": 0, "nav": 1}).limit(5000):
                    peak = n["nav"] if peak is None else max(peak, n["nav"])
                if peak:
                    t["drawdown_pct"] = round(
                        max(0.0,
                            (peak - float(current_nav)) / peak * 100), 4)
    except Exception as e:  # noqa: BLE001
        logger.error("risk-truth NAV read failed: %s", e)
        t["drawdown_pct"] = None
    # current spread — from the EA heartbeat, strategy-fresh or nothing
    fresh = telemetry_freshness(strategy_id)
    t["freshness"] = fresh
    try:
        spreads = (account or {}).get("current_spreads") or {}
        sp = spreads.get(symbol)
        if sp is None and len(symbol) > 6:
            sp = spreads.get(symbol[:6])
        age = _age_s((account or {}).get("spreads_updated_at"))
        t["spread_age_s"] = round(age, 1) if age is not None else None
        if sp is not None and age is not None and age <= fresh["spread_s"]:
            # SEC — EA spreads are self-reports; an implausibly LOW value
            # vs the cross-account consensus is treated as UNVERIFIED
            # (None ⇒ RISK_UNKNOWN on LIVE), never as passing evidence.
            consensus = await _consensus_spread(db, symbol, acct_id)
            if consensus is not None and float(sp) < 0.5 * consensus:
                t["spread_unverified"] = {"reported": float(sp),
                                          "consensus_median": consensus}
            else:
                t["spread_pips"] = float(sp)
    except Exception as e:  # noqa: BLE001
        logger.error("risk-truth spread read failed acct=%s: %s",
                     acct_id, e)
        t["spread_pips"] = None
    # recent measured fill slippage (median of last TRUSTED measurements
    # — tampered/unverified PAMM self-reports are excluded)
    try:
        slips = []
        async for tr in db.trades.find(
                {"account_id": acct_id, "slippage_checked": True,
                 "slippage_pips": {"$ne": None},
                 "pamm_slippage_verified": {"$ne": False}},
                {"slippage_pips": 1}).sort("created_at", -1).limit(10):
            slips.append(float(tr.get("slippage_pips") or 0))
        if slips:
            slips.sort()
            t["recent_slippage_pips"] = slips[len(slips) // 2]
        # consecutive losses
        streak = 0
        async for tr in db.trades.find(
                {"account_id": acct_id, "status": "closed"},
                {"pnl": 1}).sort("closed_at", -1).limit(20):
            if float(tr.get("pnl") or 0) < 0:
                streak += 1
            else:
                break
        t["consecutive_losses"] = streak
    except Exception as e:  # noqa: BLE001
        logger.error("risk-truth slippage/streak read failed acct=%s: %s",
                     acct_id, e)
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
    """Public gate — runs the closure chain, then persists a COMPLETE
    Effective Risk Decision Snapshot (envelope + telemetry + every check)
    for any governed decision and stamps its id into the context."""
    evidence: dict = {}
    res = await _authorize(db, program, account, signal, evidence)
    if res.get("mode") == "LEGACY":
        return res
    import uuid
    snap_id = f"rds_{uuid.uuid4().hex[:16]}"
    snap = {"snapshot_id": snap_id,
            "at": datetime.now(timezone.utc).isoformat(),
            "program_id": str(program.get("program_id")
                              or program.get("_id")),
            "account_id": str((account or {}).get("_id") or ""),
            "environment": evidence.get("environment"),
            "mode": res.get("mode"),
            "authorized": bool(res.get("authorized")),
            "reason": res.get("reason"),
            "signal": {"symbol": signal.get("symbol"),
                       "side": signal.get("action") or signal.get("side"),
                       "risk_pct": signal.get("risk_pct"),
                       "lot_size": signal.get("lot_size"),
                       "strategy_id": signal.get("strategy_id"),
                       "signal_id": signal.get("signal_id")},
            "envelope": evidence.get("envelope"),
            "telemetry": evidence.get("telemetry"),
            "missing_required": evidence.get("missing_required"),
            "assignment_id": (evidence.get("assignment") or {}).get(
                "assignment_id"),
            "provenance": {
                "guard_version": GUARD_VERSION,
                "execution_policy_version": _execution_policy_version(),
                "ea_version": (account or {}).get("ea_version"),
                "host_agent_version": (account or {}).get(
                    "host_agent_version"),
                "strategy_hash": (evidence.get("assignment")
                                  or {}).get("strategy_hash")},
            "checks": res.get("checks")}
    snap["hash"] = _snapshot_hash(snap)
    try:
        await db.pamm_risk_decisions.insert_one(dict(snap))
    except (TypeError, AttributeError):  # isolated unit-test db mock
        pass
    if res.get("authorized") and res.get("context") is not None:
        res["context"]["risk_snapshot_id"] = snap_id
    return res


async def _authorize(db, program: dict, account: dict, signal: dict,
                     evidence: dict) -> dict:
    checks: list = []
    program_id = str(program.get("program_id") or program.get("_id"))
    acct_id = str((account or {}).get("_id") or "")
    from broker_env import broker_environment
    env_name = broker_environment(account) if account else "PAPER"
    evidence["environment"] = env_name
    # FAIL-SAFE ASYMMETRY — closing/reducing exposure is a SAFETY EXIT:
    # it bypasses every NEW-RISK block (program state, drift, RISK_UNKNOWN,
    # envelope, canary) but still passes the structural identity chain.
    risk_reducing = is_risk_reducing(signal)
    evidence["risk_reducing"] = risk_reducing
    if risk_reducing:
        # SEC v62.7 — the label must match REALITY: there must be an open
        # position on this account (and symbol, when given) to reduce.
        # A fraudulent label is rejected; a read failure never blocks a
        # genuine safety exit (the engine cannot open on a failed DB
        # anyway, so the bypass is worthless during an outage).
        sym = str(signal.get("symbol") or "").upper()
        has_open = False
        try:
            async for tr in db.trades.find(
                    {"account_id": acct_id,
                     "status": {"$in": ["open", "pending"]}},
                    {"symbol": 1}).limit(500):
                if not sym or str(tr.get("symbol") or "").upper() == sym:
                    has_open = True
                    break
        except Exception as e:  # noqa: BLE001
            logger.critical("risk-reducing verification read failed "
                            "acct=%s: %s — allowing safety exit", acct_id, e)
            has_open = True
        if not has_open:
            return _reject("risk_reducing_label_invalid", checks,
                           {"symbol": sym or None,
                            "note": "signal is labelled risk-reducing but "
                                    "there is no open position to reduce"})
        _ok(checks, "risk_reducing",
            "safety exit verified against open positions — new-risk "
            "blocks bypassed")
    # program operating state permits new risk
    from modules.pamm.risk import trading_allowed
    allowed, why = await trading_allowed(db, program)
    if not allowed and not risk_reducing:
        return _reject("program_state_blocks_trading", checks, why)
    _ok(checks, "program_state", why)
    # Position Truth healthy (defence-in-depth; drift also pauses op-state)
    pt = program.get("position_truth") or {}
    if pt.get("status") == "drift" and not risk_reducing:
        return _reject("position_truth_drift", checks,
                       pt.get("classification"))
    _ok(checks, "position_truth", pt.get("status") or "never_checked")
    # STRATEGY GOVERNANCE MODE — sticky once migrated (fail closed)
    gov = await governance_mode(db, program)
    from modules.pamm.strategy_assignment import get_assignment
    cur = await get_assignment(db, program_id)
    a = cur.get("assignment")
    evidence["assignment"] = a
    if gov == "LEGACY":
        _ok(checks, "governance", "LEGACY — never migrated")
        return {"authorized": True, "reason": "legacy_mode",
                "mode": "LEGACY", "checks": checks,
                "context": {"pamm_program_id": program_id}}
    _ok(checks, "governance", "STRATEGY")
    # DEDICATED MANUAL OVERRIDE PATH — MFA + explicit origin enforced at
    # the route; never attributed to the strategy; envelope still applies
    # with all AVAILABLE evidence (override is the emergency escape hatch,
    # so RISK_UNKNOWN does not hard-block it)
    if signal.get("pamm_manual_override") is True:
        env = await _effective_envelope(
            db, program, (a or {}).get("risk_profile_id"))
        t = await _telemetry(db, program, account, signal)
        evidence["envelope"], evidence["telemetry"] = env, t
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
    try:
        t = await _telemetry(db, program, account, signal,
                             strategy_id=a["strategy_id"])
    except Exception as e:  # noqa: BLE001 — total collection failure
        logger.critical("risk-truth collection failed entirely: %s", e)
        t = {}
    evidence["envelope"], evidence["telemetry"] = envelope, t
    # RISK TRUTH (v62.6) — LIVE governed exposure requires COMPLETE
    # required evidence; any gap ⇒ RISK_UNKNOWN ⇒ fail closed.
    # v62.7 asymmetry: RISK_UNKNOWN blocks NEW risk, never a safety exit.
    if env_name == "LIVE" and not risk_reducing:
        missing = missing_required_telemetry(t)
        evidence["missing_required"] = missing
        if missing:
            return _reject("risk_unknown", checks,
                           {"missing_required_evidence": missing,
                            "state": "RISK_UNKNOWN",
                            "note": "new exposure is blocked until every "
                                    "required risk input is measurable "
                                    "and fresh — closes/reductions remain "
                                    "allowed"})
        # FRESH POSITION TRUTH — LIVE PAMM never trades on stale broker
        # reconciliation (window scales with strategy latency demand)
        fresh = t.get("freshness") or telemetry_freshness(a["strategy_id"])
        pt_age = _age_s(pt.get("at"))
        if pt_age is None:
            return _reject("position_truth_missing", checks,
                           {"state": "RISK_UNKNOWN",
                            "note": "LIVE PAMM requires a completed "
                                    "position-truth check"})
        if pt_age > fresh["position_truth_s"]:
            return _reject("position_truth_stale", checks,
                           {"age_s": round(pt_age, 1),
                            "max_age_s": fresh["position_truth_s"],
                            "latency_sensitivity":
                            fresh["latency_sensitivity"]})
        _ok(checks, "risk_truth",
            {"required": list(REQUIRED_TELEMETRY),
             "position_truth_age_s": round(pt_age, 1),
             "spread_age_s": t.get("spread_age_s"),
             "freshness": fresh})
    if not risk_reducing:
        vio = envelope_violations(envelope, signal, t)
        if vio:
            return _reject(vio[0]["reason"], checks, vio[0]["detail"])
    _ok(checks, "risk_envelope", envelope)
    # CANARY OPEN-RISK CAP — enforced at EXECUTION time (new risk only)
    if canary_mode and not risk_reducing:
        cv = canary_violation(t.get("open_risk_pct_sum", 0.0),
                              signal.get("risk_pct"))
        if cv:
            return _reject(cv["reason"], checks, cv["detail"])
        _ok(checks, "canary_envelope",
            {"open_risk_pct": t.get("open_risk_pct_sum"),
             "cap_pct": CANARY_MAX_OPEN_RISK_PCT})
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
