"""Adaptive daily risk budget — every strategy gets a share of the day's
total risk pool; poor performers get automatically SHRUNK, never disabled.

    pool         = daily_risk_budget_pct of equity (default 3%/day)
    base share   = allocations (trend 35 / scalp 20 / breakout 15 /
                   mean-reversion 20 / experimental 10), cfg-overridable
    perf weight  = shrink-only [0.25..1.0] from the last 14 days' realized
                   P&L per strategy (reuses rl_allocator.build_allocations)
    budget       = pool × share × weight
    spent        = Σ risk_pct of today's auto trades in that strategy

A strategy over budget gets its NEW trades shrunk to the remaining budget,
or skipped when nothing meaningful remains. Manual trades are unaffected.
"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("risk-budget")

DEFAULT_ALLOCATIONS = {"trend": 0.35, "scalp": 0.20, "breakout": 0.15,
                       "mean_reversion": 0.20, "experimental": 0.10}
SCOPE_TO_CLASS = {"hf_scalp": "scalp", "hf_scalp_fast": "scalp",
                  "scalp_fast": "scalp"}
DEFAULT_POOL_PCT = 3.0        # total % of equity riskable per day
FALLBACK_TRADE_RISK_PCT = 0.5  # legacy trades without a stored risk_pct
MIN_TRADE_RISK_PCT = 0.10      # below this remaining budget → skip


def strategy_class_of(scope: str | None, explicit: str | None = None) -> str:
    if explicit in DEFAULT_ALLOCATIONS:
        return explicit
    return SCOPE_TO_CLASS.get(str(scope or ""), "trend")


def _utc_midnight_iso() -> str:
    return datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()


def _allocations(cfg: dict) -> dict:
    alloc = dict(DEFAULT_ALLOCATIONS)
    try:
        override = cfg.get("risk_budget_allocations") or {}
        for k, v in override.items():
            if k in alloc and 0 < float(v) <= 1:
                alloc[k] = float(v)
    except Exception:
        pass
    return alloc


async def _perf_weights(db, user_id: str, account_id: str | None) -> dict:
    """Shrink-only weight per strategy class from 14d realized P&L."""
    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    q = {"user_id": user_id, "status": "closed", "origin": "auto",
         "pnl": {"$ne": None}, "closed_at": {"$gte": since}}
    if account_id:
        q["account_id"] = account_id
    pnls: dict[str, list] = {k: [] for k in DEFAULT_ALLOCATIONS}
    async for t in db.trades.find(q, {"pnl": 1, "scope": 1,
                                      "strategy_class": 1}).limit(3000):
        cls = strategy_class_of(t.get("scope"), t.get("strategy_class"))
        pnls[cls].append(float(t["pnl"]))
    try:
        from rl_allocator import build_allocations
        weights = build_allocations(pnls)
        return {k: float(weights.get(k, {}).get("weight", 1.0))
                if isinstance(weights.get(k), dict)
                else float(weights.get(k, 1.0))
                for k in DEFAULT_ALLOCATIONS}
    except Exception:  # noqa: BLE001 — fail-open to full weight (shrink is an optimisation)
        return {k: 1.0 for k in DEFAULT_ALLOCATIONS}


async def _spent_today(db, user_id: str, account_id: str | None) -> dict:
    spent = {k: 0.0 for k in DEFAULT_ALLOCATIONS}
    q = {"user_id": user_id, "origin": "auto",
         "opened_at": {"$gte": _utc_midnight_iso()}}
    if account_id:
        q["account_id"] = account_id
    async for t in db.trades.find(q, {"risk_pct": 1, "scope": 1,
                                      "strategy_class": 1}).limit(2000):
        cls = strategy_class_of(t.get("scope"), t.get("strategy_class"))
        try:
            spent[cls] += float(t.get("risk_pct") or FALLBACK_TRADE_RISK_PCT)
        except Exception:
            spent[cls] += FALLBACK_TRADE_RISK_PCT
    return {k: round(v, 3) for k, v in spent.items()}


async def budget_status(db, user_id: str, cfg: dict,
                        account_id: str | None = None) -> dict:
    pool = float((cfg or {}).get("daily_risk_budget_pct")
                 or DEFAULT_POOL_PCT)
    alloc = _allocations(cfg or {})
    alloc_basis = "static"
    # Phase 3 — dynamic multi-strategy allocation (expected return, vol,
    # drawdown, correlation, capacity, confidence → weights). Fail-open to
    # the static shares.
    if (cfg or {}).get("dynamic_allocation_enabled", True):
        try:
            from strategy_portfolio import current_allocations
            dyn = await current_allocations(db, user_id, cfg or {}, account_id)
            if dyn and dyn.get("weights"):
                alloc = dyn["weights"]
                alloc_basis = "dynamic"
        except Exception:  # noqa: BLE001
            logger.warning("dynamic allocation failed — static shares",
                           exc_info=True)
    weights = await _perf_weights(db, user_id, account_id)
    spent = await _spent_today(db, user_id, account_id)
    rows = []
    for k in DEFAULT_ALLOCATIONS:
        budget = round(pool * alloc[k] * weights[k], 3)
        rows.append({"strategy": k,
                     "allocation_pct": round(alloc[k] * 100, 1),
                     "perf_weight": round(weights[k], 2),
                     "budget_risk_pct": budget,
                     "spent_risk_pct": spent[k],
                     "remaining_risk_pct": round(max(0.0, budget - spent[k]), 3)})
    return {"pool_risk_pct": pool, "resets_at_utc_midnight": True,
            "allocation_basis": alloc_basis,
            "strategies": rows}


async def check_budget(db, user_id: str, cfg: dict, strategy: str,
                       proposed_risk_pct: float,
                       account_id: str | None = None) -> dict:
    """Verdict for one prospective trade. Shrinks instead of blocking while
    meaningful budget remains; blocks only when the strategy is spent."""
    st = await budget_status(db, user_id, cfg, account_id)
    row = next((r for r in st["strategies"] if r["strategy"] == strategy),
               None)
    if row is None:
        return {"allowed": True, "risk_pct": proposed_risk_pct,
                "reason": "unbudgeted strategy — allowed"}
    remaining = row["remaining_risk_pct"]
    if proposed_risk_pct <= remaining:
        return {"allowed": True, "risk_pct": proposed_risk_pct,
                "remaining_after": round(remaining - proposed_risk_pct, 3),
                "budget": row}
    if remaining >= MIN_TRADE_RISK_PCT:
        return {"allowed": True, "risk_pct": round(remaining, 3),
                "shrunk_from": proposed_risk_pct, "remaining_after": 0.0,
                "reason": (f"{strategy} daily budget nearly spent — trade "
                           f"shrunk {proposed_risk_pct:.2f}%→{remaining:.2f}%"),
                "budget": row}
    return {"allowed": False, "risk_pct": 0.0,
            "reason": (f"{strategy} daily risk budget exhausted "
                       f"({row['spent_risk_pct']:.2f}% of "
                       f"{row['budget_risk_pct']:.2f}% used) — resets at "
                       f"UTC midnight"),
            "budget": row}
