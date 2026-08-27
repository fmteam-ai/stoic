"""AI intervention effectiveness — did the brain's blocks and downgrades
actually help? Honest accounting:

  · SKIP/blocked decisions have NO observable counterfactual — reported
    as counts only, never as invented "saved money".
  · REDUCE is measurable: reduced trades still executed, so we compare
    realized R of the REDUCE cohort vs the TRADE cohort, and compute the
    $ effect of the size cut on realized winners vs losers."""
from datetime import datetime, timedelta, timezone

MAX_DOCS = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def effectiveness(db, user_id: str, days: int = 30) -> dict:
    since = (datetime.now(timezone.utc)
             - timedelta(days=max(1, min(int(days), 90)))).isoformat()
    counts = {"TRADE": 0, "REDUCE": 0, "SKIP": 0}
    hard_gates = 0
    meta_by_id: dict = {}
    async for m in db.meta_decisions.find(
            {"user_id": user_id, "at": {"$gte": since}},
            {"decision": 1, "decision_id": 1, "risk_multiplier": 1,
             "uncertainty_detail.hard_gate": 1}).limit(MAX_DOCS):
        d = str(m.get("decision") or "")
        if d in counts:
            counts[d] += 1
        if (m.get("uncertainty_detail") or {}).get("hard_gate"):
            hard_gates += 1
        if m.get("decision_id"):
            meta_by_id[m["decision_id"]] = (
                d, float(m.get("risk_multiplier") or 0))
    twin = {"TRADE": 0, "REDUCE": 0, "SKIP": 0}
    async for t in db.pretrade_twin.find(
            {"user_id": user_id, "at": {"$gte": since}},
            {"verdict": 1}).limit(MAX_DOCS):
        v = str(t.get("verdict") or "")
        if v in twin:
            twin[v] += 1
    alpha_modes: dict = {}
    async for a in db.execution_alpha_decisions.find(
            {"user_id": user_id, "at": {"$gte": since}},
            {"mode": 1}).limit(MAX_DOCS):
        m = str(a.get("mode") or "UNKNOWN")
        alpha_modes[m] = alpha_modes.get(m, 0) + 1
    # measurable cohorts — realized R of REDUCE vs TRADE decisions
    from outcome_attribution import result_r
    cohorts = {"TRADE": {"n": 0, "sum_r": 0.0},
               "REDUCE": {"n": 0, "sum_r": 0.0}}
    saved_usd = forgone_usd = 0.0
    async for t in db.trades.find(
            {"user_id": user_id, "status": "closed",
             "closed_at": {"$gte": since},
             "decision_id": {"$exists": True}},
            {"decision_id": 1, "pnl": 1, "entry_price": 1, "stop_loss": 1,
             "exit_price": 1, "action": 1}).limit(MAX_DOCS):
        meta = meta_by_id.get(t.get("decision_id"))
        if not meta or meta[0] not in cohorts:
            continue
        decision, mult = meta
        r, _src = result_r(t)
        cohorts[decision]["n"] += 1
        cohorts[decision]["sum_r"] += r
        if decision == "REDUCE" and 0 < mult < 1:
            pnl = float(t.get("pnl") or 0)
            delta = pnl * (1 / mult - 1)  # extra pnl had it been full size
            if pnl < 0:
                saved_usd += -delta
            else:
                forgone_usd += delta
    avg = {k: (round(v["sum_r"] / v["n"], 3) if v["n"] else None)
           for k, v in cohorts.items()}
    justified = (avg["REDUCE"] is not None and avg["TRADE"] is not None
                 and avg["REDUCE"] < avg["TRADE"])
    return {
        "days": days,
        "meta": {"counts": counts, "hard_gates": hard_gates},
        "twin": twin,
        "execution_alpha_modes": alpha_modes,
        "reduce_effect": {
            "trade_cohort": {"n": cohorts["TRADE"]["n"],
                             "avg_r": avg["TRADE"]},
            "reduce_cohort": {"n": cohorts["REDUCE"]["n"],
                              "avg_r": avg["REDUCE"]},
            "saved_on_losers_usd": round(saved_usd, 2),
            "forgone_on_winners_usd": round(forgone_usd, 2),
            "net_usd": round(saved_usd - forgone_usd, 2),
            "downgrades_justified": justified if avg["REDUCE"] is not None
            else None},
        "skips": {"n": counts["SKIP"], "hard_gates": hard_gates,
                  "note": "blocked trades have no observable "
                          "counterfactual — counts only, never invented "
                          "'saved money'"},
        "at": _now()}
