"""Phase 1.3 — Digital Twin stress expansion.

Replays the twin's decision set under adverse execution scenarios so
resilience is tested without risking live accounts. Perturbations are
applied at the R level with documented, conservative assumptions.
"""
from datetime import datetime, timedelta, timezone

SCENARIOS = {
    "latency_spike": "every fill pays +0.05R extra slippage",
    "delayed_fill": "winners capture 10% less of the move (late entry)",
    "spread_explosion": "spread doubles — +0.12R cost per trade",
    "liquidity_drop": "only 50% of intended size fills",
    "broker_outage": "every 5th decision is never filled",
    "market_gap": "losers gap through the stop — lose 1.3R instead of 1.0R",
}


def apply_scenario(rs: list, name: str) -> list:
    if name == "latency_spike":
        return [r - 0.05 for r in rs]
    if name == "delayed_fill":
        return [r * 0.9 if r > 0 else r for r in rs]
    if name == "spread_explosion":
        return [r - 0.12 for r in rs]
    if name == "liquidity_drop":
        return [r * 0.5 for r in rs]
    if name == "broker_outage":
        return [r for i, r in enumerate(rs) if i % 5 != 4]
    if name == "market_gap":
        return [r * 1.3 if r < 0 else r for r in rs]
    raise ValueError(f"unknown scenario {name}")


async def stress(db, user_id: str, days: int = 30) -> dict:
    from shadow_benchmark import _executed_rs, replay_rejections
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    executed = [t["r"] for t in await _executed_rs(db, user_id, since)]
    replayed = [d["r"] for d in
                await replay_rejections(db, user_id, since, limit=500)]
    rs = executed + replayed
    clean = round(sum(rs), 2) if rs else 0.0
    scenarios = []
    for name, desc in SCENARIOS.items():
        stressed = apply_scenario(rs, name) if rs else []
        net = round(sum(stressed), 2)
        delta = round(net - clean, 2)
        scenarios.append({
            "scenario": name, "description": desc, "net_r": net,
            "delta_r": delta,
            "verdict": ("RESILIENT" if not rs or net >= clean * 0.5
                        or net >= clean else "DEGRADED")
            if clean >= 0 else
            ("RESILIENT" if net >= clean * 1.5 else "DEGRADED")})
    worst = min(scenarios, key=lambda s: s["net_r"]) if scenarios else None
    return {"days": days, "decisions": len(rs), "clean_net_r": clean,
            "scenarios": scenarios,
            "worst_case": (worst or {}).get("scenario"),
            "note": ("Executed trades use realized R; intercepted decisions "
                     "are replayed on actual bars, then perturbed with "
                     "documented adverse-execution assumptions.")}
