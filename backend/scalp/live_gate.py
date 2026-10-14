"""Scalp subsystem — v1.60.29 A1(c): the LIVE gate is code, not policy.

The engine may send real orders (`demo_live`) only when the SHADOW record for that user/symbol shows, over at least
MIN_LABELED labeled samples, a positive net expectancy AFTER costs and a profit factor above 1. Until then every
`demo_live` request is executed as shadow and the status says why. XAUUSD (A1(d)) additionally requires EURUSD to have
passed — the new setup must prove itself on the cheapest instrument first.
"""
from __future__ import annotations

import os

MIN_LABELED = int(os.environ.get("SCALP_LIVE_MIN_LABELED", "300"))
MIN_EXPECTANCY_PIPS = float(os.environ.get("SCALP_LIVE_MIN_EXPECTANCY_PIPS", "0.0"))
MIN_PROFIT_FACTOR = float(os.environ.get("SCALP_LIVE_MIN_PF", "1.0"))
PROVING_SYMBOL = "EURUSD"


def evaluate(stats: dict) -> dict:
    """Pure. stats = {labeled, net_pips_sum, gross_win_pips, gross_loss_pips} from the SHADOW dataset."""
    n = int(stats.get("labeled") or 0)
    net = float(stats.get("net_pips_sum") or 0.0)
    wins = float(stats.get("gross_win_pips") or 0.0)
    losses = abs(float(stats.get("gross_loss_pips") or 0.0))
    expectancy = (net / n) if n else None
    pf = (wins / losses) if losses > 0 else (float("inf") if wins > 0 else None)
    reasons = []
    if n < MIN_LABELED:
        reasons.append(f"{n}/{MIN_LABELED} labeled shadow samples")
    if expectancy is None or expectancy <= MIN_EXPECTANCY_PIPS:
        reasons.append(f"net expectancy after costs {expectancy if expectancy is not None else '—'} pips/trade (need > {MIN_EXPECTANCY_PIPS:g})")
    if pf is None or pf <= MIN_PROFIT_FACTOR:
        reasons.append(f"profit factor {('%.2f' % pf) if pf not in (None, float('inf')) else (pf or '—')} (need > {MIN_PROFIT_FACTOR:g})")
    return {"eligible": not reasons, "labeled": n, "expectancy_pips": round(expectancy, 3) if expectancy is not None else None,
            "profit_factor": (round(pf, 3) if pf not in (None, float("inf")) else pf), "reasons": reasons,
            "thresholds": {"min_labeled": MIN_LABELED, "min_expectancy_pips": MIN_EXPECTANCY_PIPS, "min_pf": MIN_PROFIT_FACTOR}}


async def shadow_stats(db, user_id: str, symbol: str, setup: str | None = "range_break_momentum_v2") -> dict:
    """Labeled SHADOW outcomes for one user/symbol (only the current setup's rows count toward the gate)."""
    q = {"user_id": user_id, "symbol": symbol, "outcome.result": {"$exists": True},
         "execution_mode": {"$in": ["shadow", None]}}
    if setup:
        q["setup.setup"] = setup
    out = {"labeled": 0, "net_pips_sum": 0.0, "gross_win_pips": 0.0, "gross_loss_pips": 0.0}
    async for d in db.scalp_decisions.find(q, {"outcome": 1}):
        net = float((d.get("outcome") or {}).get("net_pips") or 0.0)
        out["labeled"] += 1
        out["net_pips_sum"] += net
        if net > 0:
            out["gross_win_pips"] += net
        else:
            out["gross_loss_pips"] += net
    return out


async def live_eligibility(db, user_id: str, symbol: str) -> dict:
    """The verdict the engine and the UI both use."""
    own = evaluate(await shadow_stats(db, user_id, symbol))
    if symbol.upper() != PROVING_SYMBOL:
        proving = evaluate(await shadow_stats(db, user_id, PROVING_SYMBOL))
        if not proving["eligible"]:
            own["eligible"] = False
            own["reasons"] = [f"{PROVING_SYMBOL} must pass the live gate first"] + own["reasons"]
        own["proving_symbol"] = {"symbol": PROVING_SYMBOL, **proving}
    return own
