"""Unified Trade Intelligence Report — ONE document tying the full chain
together: decisions → interventions → execution → outcomes → learning
health. Everything is computed from what actually happened; nothing is
estimated without saying so."""
from datetime import datetime, timedelta, timezone

MAX_DOCS = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def report(db, user_id: str, days: int = 30) -> dict:
    days = max(1, min(int(days), 90))
    since = (datetime.now(timezone.utc)
             - timedelta(days=days)).isoformat()
    # ── decision funnel ─────────────────────────────────────────────
    decisions_minted = await db.decision_contexts.count_documents(
        {"user_id": user_id, "at": {"$gte": since}})
    meta_counts = {"TRADE": 0, "REDUCE": 0, "SKIP": 0}
    async for m in db.meta_decisions.find(
            {"user_id": user_id, "at": {"$gte": since}},
            {"decision": 1}).limit(MAX_DOCS):
        d = str(m.get("decision") or "")
        if d in meta_counts:
            meta_counts[d] += 1
    opened = await db.trades.count_documents(
        {"user_id": user_id, "opened_at": {"$gte": since}})
    # ── realized outcomes ───────────────────────────────────────────
    from outcome_attribution import result_r
    n = wins = 0
    sum_r = sum_pnl = 0.0
    async for t in db.trades.find(
            {"user_id": user_id, "status": "closed",
             "closed_at": {"$gte": since}},
            {"pnl": 1, "entry_price": 1, "stop_loss": 1, "exit_price": 1,
             "action": 1}).limit(MAX_DOCS):
        r, _src = result_r(t)
        pnl = float(t.get("pnl") or 0)
        n += 1
        wins += 1 if pnl > 0 else 0
        sum_r += r
        sum_pnl += pnl
    outcomes = {"closed": n, "win_rate": round(wins / n, 3) if n else None,
                "avg_r": round(sum_r / n, 3) if n else None,
                "total_r": round(sum_r, 2), "total_pnl": round(sum_pnl, 2)}
    # ── execution quality ───────────────────────────────────────────
    from latency_profiler import latency_summary
    lat = await latency_summary(db, days=min(days, 90), user_id=user_id)
    execution = {"traced_trades": lat.get("traced_trades"),
                 "total_trades": lat.get("total_trades"),
                 "unknown_rate": lat.get("unknown_rate"),
                 "worst_groups": lat.get("groups", [])[:5]}
    # ── uncertainty calibration (realized conformal coverage) ───────
    from uncertainty_engine import _sample_r, realized_coverage
    rs = await _sample_r(db, user_id, None, None)
    coverage = (realized_coverage(list(reversed(rs))) if len(rs) >= 20
                else {"evaluated": 0, "coverage": None, "ok": True,
                      "note": f"only {len(rs)} comparable trades"})
    # ── interventions ───────────────────────────────────────────────
    from intervention_metrics import effectiveness
    interventions = await effectiveness(db, user_id, days=days)
    # ── learning health ─────────────────────────────────────────────
    strategies = [{"scope": h.get("scope"), "state": h.get("state")}
                  async for h in db.strategy_health.find(
                      {"user_id": user_id},
                      {"scope": 1, "state": 1}).limit(50)]
    from degraded_intelligence import status as degraded_status
    deg = await degraded_status(db)
    return {
        "days": days, "user_id": user_id, "generated_at": _now(),
        "funnel": {"decisions_minted": decisions_minted,
                   "meta": meta_counts, "trades_opened": opened},
        "outcomes": outcomes,
        "execution": execution,
        "uncertainty_calibration": coverage,
        "interventions": interventions,
        "learning": {"strategy_health": strategies,
                     "degraded_mode": deg.get("mode"),
                     "failing_subsystems": deg.get("failing", [])},
    }
