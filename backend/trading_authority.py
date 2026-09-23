"""Global Trading Authority (v56 §16) — ONE first-class entity that
answers "may STOIC open new exposure right now?". Computed as the MOST
RESTRICTIVE level across all domains (platform, account, broker,
infrastructure, risk, PAMM, execution, position truth). Strategies never
decide executability — the authority does, at the single execution
choke point (MT5BridgeEngine.execute).

Levels (ordered): FULL < REDUCED < CLOSE_ONLY < PAUSED < EMERGENCY < LOCKED
Enforcement (audit P0-1): EVERY domain is enforced at the choke point via
the SAME canonical snapshot the UI renders — enforced_level == level.
With an account the snapshot is account-scoped (PAMM/broker/position-truth
domains restrict only the accounts bound to the affected program); without
an account it is the platform-wide ops view.
A domain whose state cannot be evaluated degrades to CLOSE_ONLY — the
system never infers safety from missing information."""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("trading.authority")

LEVELS = ["FULL", "REDUCED", "CLOSE_ONLY", "PAUSED", "EMERGENCY", "LOCKED"]
_UNAVAILABLE = {"level": "CLOSE_ONLY",
                "reason": "domain state unavailable — never infer safety"}


def level_severity(level: str) -> int:
    return LEVELS.index(level)


def worst(*levels: str) -> str:
    return max(levels, key=level_severity)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ago(seconds: int) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds)).isoformat()


async def platform_domain(db, account: dict | None = None) -> dict:
    """Platform-global: the ops kill switch applies to every account."""
    doc = await db.platform_state.find_one({"_id": "trading_authority"})
    if doc and doc.get("level") in LEVELS and doc["level"] != "FULL":
        return {"level": doc["level"],
                "reason": doc.get("reason") or "platform override",
                "set_by": doc.get("set_by")}
    return {"level": "FULL", "reason": "no platform restriction"}


async def account_domain(db, account: dict | None = None) -> dict:
    if not account:
        return {"level": "FULL", "reason": "no account context"}
    # audit v4 P0-2/P0-3 — an EA terminal identity mismatch quarantines the
    # account: no open authority until it is explicitly re-paired.
    if account.get("broker_account_mismatch"):
        return {"level": "LOCKED",
                "reason": "EA terminal identity mismatch — account "
                          "quarantined until re-paired"}
    # AT-01 — authority agrees with worker selection and the execution choke
    # point: enablement must be EXPLICITLY true (missing/false = OFF).
    if "_id" in account and account.get("trading_enabled") is not True:
        return {"level": "LOCKED",
                "reason": "trading_enabled is not explicitly true — account OFF"}
    lvl = account.get("trading_authority")
    if lvl in LEVELS and lvl != "FULL":
        return {"level": lvl, "reason": "account-level restriction"}
    return {"level": "FULL", "reason": "account unrestricted"}


async def infrastructure_domain(db, account: dict | None = None) -> dict:
    if account is not None:
        if account.get("mode") == "paper":
            return {"level": "FULL", "reason": "paper account — no "
                                                "terminal dependency"}
        hb = str(account.get("last_heartbeat") or "")
        if hb and hb >= _ago(180):
            return {"level": "FULL", "reason": "terminal heartbeat fresh"}
        if hb:
            return {"level": "REDUCED",
                    "reason": "terminal heartbeat stale (>180s)"}
        return {"level": "REDUCED", "reason": "terminal never connected"}
    n = await db.accounts.count_documents(
        {"last_heartbeat": {"$gte": _ago(600)}})
    if n:
        return {"level": "FULL", "reason": f"{n} live terminal(s)"}
    return {"level": "REDUCED",
            "reason": "no live terminal heartbeats in 10m"}


def _acct_id(account: dict | None) -> str:
    return str((account or {}).get("_id") or (account or {}).get("account_id")
               or "")


async def _bound_programs(db, account: dict) -> list:
    """PAMM programs this account executes for (master binding or the
    account-declared program). Tenant scoping (audit P1-5): a PAMM state
    only restricts the accounts bound to that program."""
    ors = [{"master_account_id": _acct_id(account)}]
    if account.get("pamm_program_id"):
        ors.append({"program_id": str(account["pamm_program_id"])})
    return [p async for p in db.pamm_programs.find(
        {"$or": ors, "status": {"$nin": ["archived", "closed"]}},
        {"program_id": 1, "op_state": 1, "name": 1, "partner_id": 1})]


async def broker_domain(db, account: dict | None = None) -> dict:
    inc_q: dict = {"status": "open", "type": "flatten_failed"}
    partner_q: dict = {"status": {"$nin": ["active"]}}
    if account is not None:
        progs = await _bound_programs(db, account)
        if not progs:
            return {"level": "FULL",
                    "reason": "no broker-partner dependency"}
        inc_q["program_id"] = {"$in": [p["program_id"] for p in progs]}
        partner_q["partner_id"] = {"$in": [p.get("partner_id")
                                           for p in progs]}
    inc = await db.pamm_incidents.count_documents(inc_q)
    if inc:
        return {"level": "CLOSE_ONLY",
                "reason": f"{inc} open flatten-failed incident(s)"}
    down = await db.broker_partners.count_documents(partner_q)
    if down:
        return {"level": "REDUCED",
                "reason": f"{down} broker partner(s) inactive"}
    return {"level": "FULL", "reason": "broker links healthy"}


async def risk_domain(db, account: dict | None = None) -> dict:
    q: dict = {"blocked_at": {"$gte": _ago(1800)}}
    if account is not None:
        q["account_id"] = _acct_id(account)
    n = await db.safety_blocks.count_documents(q)
    if n:
        return {"level": "REDUCED",
                "reason": f"safety guardian refused {n} trade(s) in 30m"}
    return {"level": "FULL", "reason": "no recent safety refusals"}


_PAMM_MAP = {"running": "FULL", "risk_reduced": "REDUCED",
             "new_trades_paused": "CLOSE_ONLY",
             "broker_uncertain": "CLOSE_ONLY",
             "close_risk_only": "CLOSE_ONLY",
             "emergency_flatten": "EMERGENCY", "locked": "LOCKED"}


async def pamm_domain(db, account: dict | None = None) -> dict:
    lvl, why = "FULL", "all programs running"
    if account is not None:
        progs = await _bound_programs(db, account)
        if not progs:
            return {"level": "FULL", "reason": "account not PAMM-bound"}
    else:
        progs = [p async for p in db.pamm_programs.find(
            {"status": "active"}, {"op_state": 1, "name": 1})]
    for p in progs:
        mapped = _PAMM_MAP.get(p.get("op_state") or "running", "CLOSE_ONLY")
        if level_severity(mapped) > level_severity(lvl):
            lvl = mapped
            why = (f"program '{p.get('name')}' is "
                   f"{(p.get('op_state') or '').replace('_', ' ')}")
    return {"level": lvl, "reason": why}


async def execution_domain(db, account: dict | None = None) -> dict:
    """Round 6 P1 — the execution-truth policy IS the canonical decision:
    UNKNOWN blocks immediately, broker-accepted non-terminal states past
    their state SLA and any position mismatch enforce CLOSE_ONLY here, in
    release readiness and in user readiness alike."""
    from execution_truth import (authority_for, position_mismatches,
                                 unresolved_backlog)
    now = datetime.now(timezone.utc)
    ids = [_acct_id(account)] if account is not None else None
    backlog = await unresolved_backlog(db, now, account_ids=ids)
    mismatches = await position_mismatches(db, now, account_ids=ids)
    return authority_for(backlog["exemplars"], mismatches, backlog["by_status"])


async def position_truth_domain(db, account: dict | None = None) -> dict:
    q: dict = {"status": "open", "type": "position_drift"}
    if account is not None:
        progs = await _bound_programs(db, account)
        q["program_id"] = {"$in": [p["program_id"] for p in progs]}
    n = await db.pamm_incidents.count_documents(q)
    if n:
        return {"level": "CLOSE_ONLY",
                "reason": f"{n} open position drift incident(s)"}
    if account is not None:
        # the SAME canonical per-account truth the readiness UI shows
        from state_contract import account_truth
        open_local = await db.trades.count_documents(
            {"account_id": _acct_id(account), "status": "open"})
        truth = account_truth(account, open_local)["position_truth"]
        if truth != "FRESH":
            return {"level": "CLOSE_ONLY",
                    "reason": f"canonical position truth is {truth} — "
                              "broker count cannot be confirmed"}
    return {"level": "FULL", "reason": "positions reconciled"}


_DOMAINS = {
    "platform": platform_domain,
    "broker": broker_domain,
    "risk": risk_domain,
    "pamm": pamm_domain,
    "execution": execution_domain,
    "position_truth": position_truth_domain,
    "infrastructure": infrastructure_domain,
    "account": account_domain,
}
# Typed registry contract (audit v5 P0-1): every domain is
# `async def domain(db, account=None) -> dict` and declares its scope.
# Argument errors are NEVER control flow — conformance is asserted at
# import time so a malformed domain fails the process, not the trade.
DOMAIN_SCOPE = {"platform": "platform_global", "broker": "account_bound",
                "risk": "account_bound", "pamm": "account_bound",
                "execution": "account_bound",
                "position_truth": "account_bound",
                "infrastructure": "account_bound", "account": "account_bound"}
# domains whose FRESH/FULL state is a precondition for RESIZING (REDUCED)
HARD_TRUTH_DOMAINS = ("position_truth", "broker", "execution")


def assert_domain_protocol(domains: dict | None = None) -> None:
    import inspect
    reg = domains if domains is not None else _DOMAINS
    for name, fn in reg.items():
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"authority domain '{name}' must be async")
        params = list(inspect.signature(fn).parameters.values())
        if len(params) < 2 or params[1].name != "account" \
                or params[1].default is not None:
            raise TypeError(f"authority domain '{name}' must accept "
                            f"(db, account=None); got {params}")
        if name not in DOMAIN_SCOPE:
            raise TypeError(f"authority domain '{name}' has no scope entry")


assert_domain_protocol()


async def compute_authority(db, account: dict | None = None) -> dict:
    """ONE canonical snapshot. With an account it is account-scoped (the
    decision the choke point enforces); without, it is the platform-wide
    ops view. enforced_level == level: every domain is enforced."""
    import uuid
    domains = {}
    for name, fn in _DOMAINS.items():
        try:
            domains[name] = await fn(db, account)
        except Exception as e:
            logger.error("authority domain %s failed: %s", name, e)
            domains[name] = dict(_UNAVAILABLE)
    effective = worst(*(d["level"] for d in domains.values()))
    unavailable = [k for k, v in domains.items()
                   if v.get("reason") == _UNAVAILABLE["reason"]]
    return {"snapshot_id": f"authsnap_{uuid.uuid4().hex[:12]}",
            "level": effective, "enforced_level": effective,
            "restricted": effective != "FULL", "domains": domains,
            "domain_scope": DOMAIN_SCOPE,
            "unavailable_domains": unavailable,
            "hard_truth_fresh": all(domains[k]["level"] == "FULL"
                                    for k in HARD_TRUTH_DOMAINS),
            "scope": "account" if account is not None else "platform",
            "account_id": _acct_id(account) or None,
            "reasons": [f"{k}: {v['reason']}" for k, v in domains.items()
                        if v["level"] != "FULL"],
            "computed_at": _now()}


async def enforce_new_trade(db, account: dict | None = None) -> dict:
    """Blocking gate at the execution choke point — consumes the SAME
    canonical snapshot the UI shows (audit P0-1). Any domain at CLOSE_ONLY
    or worse refuses new exposure; an unavailable domain refuses (never
    infer safety); REDUCED halves volume ONLY while every hard-truth
    domain (position truth / broker / execution) is FULL."""
    snap = await compute_authority(db, account)
    lvl = snap["level"]
    reasons = list(snap["reasons"])
    base = {"level": lvl, "reasons": reasons,
            "snapshot_id": snap["snapshot_id"],
            "domains": {k: v["level"] for k, v in snap["domains"].items()}}
    if level_severity(lvl) >= level_severity("CLOSE_ONLY"):
        return {"ok": False, **base}
    if snap["unavailable_domains"]:
        return {"ok": False, **base, "level": "CLOSE_ONLY",
                "reasons": reasons + [
                    f"domain(s) unavailable: "
                    f"{', '.join(snap['unavailable_domains'])} — "
                    "refusing new exposure"]}
    if lvl == "REDUCED":
        if not snap["hard_truth_fresh"]:
            return {"ok": False, **base, "level": "CLOSE_ONLY",
                    "reasons": reasons + [
                        "REDUCED requires fresh position/broker/"
                        "execution truth — refusing new exposure"]}
        return {"ok": True, **base, "reduce_factor": 0.5}
    return {"ok": True, **base, "level": "FULL", "reasons": []}


async def set_platform_level(db, level: str, reason: str,
                             actor: str) -> dict:
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")
    prev = await db.platform_state.find_one({"_id": "trading_authority"})
    prev_level = (prev or {}).get("level") or "FULL"
    doc = {"level": level, "reason": str(reason or "")[:300],
           "set_by": actor, "set_at": _now(), "previous": prev_level}
    await db.platform_state.update_one({"_id": "trading_authority"},
                                       {"$set": doc}, upsert=True)
    await db.authority_audit.insert_one(
        {"at": _now(), "actor": actor, "from": prev_level, "to": level,
         "reason": doc["reason"]})
    logger.warning("PLATFORM TRADING AUTHORITY %s → %s by %s (%s)",
                   prev_level, level, actor, reason)
    return doc
