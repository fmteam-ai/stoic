"""PAMM Risk Engine (Phase 9) — loss caps, drawdown, exposure, correlation.
STOIC gates the master account: breach → halt or flatten (per-limit action).
The broker executes; STOIC only decides whether trading is ALLOWED."""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("pamm.risk")

DEFAULT_LIMITS = {
    "daily_loss_pct": {"enabled": True, "threshold": 5.0, "action": "halt"},
    "weekly_loss_pct": {"enabled": True, "threshold": 10.0, "action": "halt"},
    "monthly_loss_pct": {"enabled": True, "threshold": 15.0, "action": "halt"},
    "max_drawdown_pct": {"enabled": True, "threshold": 20.0,
                         "action": "flatten"},
    "max_exposure_pct": {"enabled": True, "threshold": 200.0,
                         "action": "halt"},
    "max_correlated_positions": {"enabled": True, "threshold": 3,
                                 "action": "halt"},
    "news_filter": {"enabled": True, "blackout_before_min": 30,
                    "blackout_after_min": 15, "min_impact": "high"},
}
ACTIONS = {"halt", "flatten"}
LOSS_PERIODS = (("daily_loss_pct", "daily"), ("weekly_loss_pct", "weekly"),
                ("monthly_loss_pct", "monthly"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_limits(program: dict) -> dict:
    merged = {k: dict(v) for k, v in DEFAULT_LIMITS.items()}
    for k, v in (program.get("risk_limits") or {}).items():
        if k in merged and isinstance(v, dict):
            merged[k].update(v)
    return merged


def validate_limits_patch(patch: dict) -> dict:
    """Sanitize an admin/manager PUT payload; raises ValueError."""
    clean = {}
    for k, v in (patch or {}).items():
        if k not in DEFAULT_LIMITS or not isinstance(v, dict):
            raise ValueError(f"unknown risk limit: {k}")
        entry = {}
        if "enabled" in v:
            entry["enabled"] = bool(v["enabled"])
        if "action" in v:
            if v["action"] not in ACTIONS:
                raise ValueError(f"action must be one of {sorted(ACTIONS)}")
            entry["action"] = v["action"]
        if "threshold" in v:
            t = float(v["threshold"])
            if t <= 0:
                raise ValueError(f"{k}.threshold must be positive")
            entry["threshold"] = t
        for f in ("blackout_before_min", "blackout_after_min"):
            if f in v:
                entry[f] = max(0, min(240, int(v[f])))
        if "min_impact" in v:
            imp = str(v["min_impact"]).lower()
            if imp not in ("low", "medium", "high"):
                raise ValueError("min_impact must be low|medium|high")
            entry["min_impact"] = imp
        clean[k] = entry
    return clean


def _period_start(period: str) -> str:
    now = datetime.now(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        return day.isoformat()
    if period == "weekly":
        return (day - timedelta(days=now.weekday())).isoformat()
    return day.replace(day=1).isoformat()


async def _baseline_nav(db, program_id: str, since_iso: str) -> float | None:
    """NAV at period start: last snapshot before start, else first within."""
    prev = await db.pamm_nav_snapshots.find_one(
        {"program_id": program_id, "at": {"$lt": since_iso}},
        {"_id": 0, "nav": 1}, sort=[("at", -1)])
    if prev:
        return prev["nav"]
    first = await db.pamm_nav_snapshots.find_one(
        {"program_id": program_id, "at": {"$gte": since_iso}},
        {"_id": 0, "nav": 1}, sort=[("at", 1)])
    return first["nav"] if first else None


def _check(limit: str, cfg: dict, value, note: str = "") -> dict:
    breached = (bool(cfg.get("enabled")) and value is not None
                and float(value) >= float(cfg["threshold"]))
    return {"limit": limit, "enabled": bool(cfg.get("enabled")),
            "value": value, "threshold": cfg["threshold"],
            "action": cfg.get("action", "halt"), "breached": breached,
            "note": note}


async def evaluate_program(db, program: dict) -> dict:
    """Pure evaluation — computes every check, takes NO action."""
    pid = program["program_id"]
    limits = get_limits(program)
    current_nav = (program.get("last_nav") or {}).get("nav")
    if current_nav is None:
        latest = await db.pamm_nav_snapshots.find_one(
            {"program_id": pid}, {"_id": 0, "nav": 1}, sort=[("at", -1)])
        current_nav = latest["nav"] if latest else None

    checks = []
    for key, period in LOSS_PERIODS:
        base = await _baseline_nav(db, pid, _period_start(period))
        loss = None
        if base and current_nav is not None:
            loss = round(max(0.0, (base - current_nav) / base * 100), 3)
        checks.append(_check(key, limits[key], loss,
                             note=f"baseline={base}"))

    peak = None
    async for n in db.pamm_nav_snapshots.find(
            {"program_id": pid}, {"_id": 0, "nav": 1}).sort("at", 1):
        peak = n["nav"] if peak is None else max(peak, n["nav"])
    dd = None
    if peak and current_nav is not None:
        dd = round(max(0.0, (peak - current_nav) / peak * 100), 3)
    checks.append(_check("max_drawdown_pct", limits["max_drawdown_pct"], dd,
                         note=f"peak={peak}"))

    positions, pos_err = [], None
    try:
        from services.broker_gateway.pamm_api import get_adapter
        adapter = await get_adapter(db, program["partner_id"])
        positions = await adapter.get_positions(program["broker_program_id"])
    except Exception as e:  # broker unreachable → exposure unknown, not breach
        pos_err = str(e)[:120]
    exposure = sum(abs(float(p.get("exposure_usd") or p.get("notional") or 0))
                   for p in positions)
    exp_pct = (round(exposure / current_nav * 100, 3)
               if current_nav and not pos_err else None)
    checks.append(_check("max_exposure_pct", limits["max_exposure_pct"],
                         exp_pct, note=pos_err or f"open={len(positions)}"))

    buckets: dict = {}
    for p in positions:
        sym = str(p.get("symbol") or "").upper()
        for cur in (sym[:3], sym[3:6]):
            if len(cur) == 3 and cur.isalpha():
                buckets[cur] = buckets.get(cur, 0) + 1
    max_corr = max(buckets.values(), default=0)
    top = max(buckets, key=buckets.get) if buckets else None
    checks.append(_check("max_correlated_positions",
                         limits["max_correlated_positions"],
                         max_corr if not pos_err else None,
                         note=f"currency={top}" if top else ""))

    return {"program_id": pid, "nav": current_nav, "at": _now(),
            "checks": checks,
            "breached": [c for c in checks if c["breached"]]}


async def run_risk_check(db, program: dict,
                         actor: str = "risk-engine") -> dict:
    """Evaluate + ENFORCE: on breach, halt (pause) or flatten + halt,
    record the breach, emit event and raise an admin alert."""
    from modules.pamm.events import emit_event
    from modules.pamm.risk.news import news_blackout_status

    result = await evaluate_program(db, program)
    result["news"] = await news_blackout_status(
        db, get_limits(program)["news_filter"])
    breached = result["breached"]
    result["action_taken"] = None
    if not breached or program.get("risk_breach"):
        return result

    names = [c["limit"] for c in breached]
    reason = f"RISK: {', '.join(names)}"
    action = ("flatten" if any(c["action"] == "flatten" for c in breached)
              else "halt")
    # escalate op-state (automation may only move to a SAFER state);
    # set_op_state pauses the broker and, for emergency_flatten, flattens.
    from modules.pamm.risk.states import (op_state_of, set_op_state,
                                          severity)
    target = "emergency_flatten" if action == "flatten" else "new_trades_paused"
    flattened = 0
    if severity(target) > severity(op_state_of(program)):
        if action == "flatten":
            positions_before = 0
            try:
                from services.broker_gateway.pamm_api import get_adapter
                adapter = await get_adapter(db, program["partner_id"])
                positions_before = len(await adapter.get_positions(
                    program["broker_program_id"]))
            except Exception:
                pass
            flattened = positions_before
        await set_op_state(db, program, target, actor,
                           reason=reason, source="risk-engine")
    else:
        from services.broker_gateway.manager_api import pause_program
        await pause_program(db, program, actor, reason=reason)
    breach_doc = {"limits": names, "action": action, "at": _now(),
                  "actor": actor, "flattened": flattened,
                  "details": [{k: c[k] for k in
                               ("limit", "value", "threshold", "action")}
                              for c in breached]}
    await db.pamm_programs.update_one(
        {"program_id": program["program_id"]},
        {"$set": {"risk_breach": breach_doc}})
    await emit_event(db, "RiskLimitBreached",
                     {"program_id": program["program_id"], "limits": names,
                      "action": action, "flattened": flattened})
    await db.pamm_notifications.insert_one(
        {"type": "RiskLimitBreached", "program_id": program["program_id"],
         "at": _now(), "seen": False,
         "summary": f"Risk breach on {program.get('name')}: "
                    f"{', '.join(names)} → {action.upper()}"})
    logger.warning("PAMM risk breach on %s: %s → %s",
                   program["program_id"], names, action)
    result["action_taken"] = action
    result["risk_breach"] = breach_doc
    return result
