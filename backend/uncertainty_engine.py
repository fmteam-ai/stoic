"""Uncertainty Engine — teach STOIC to say "I don't know". Bootstrap
edge intervals over comparable alpha-clean history + independent-model
disagreement. A trade is taken only when the LOWER bound of expected
edge clears uncertainty + transaction costs — not merely p > 0.5."""
import random
from datetime import datetime, timedelta, timezone

MIN_SAMPLES_HARD_GATE = 30   # below this the engine advises, never blocks
BOOTSTRAP_ITERS = 400
COST_R_DEFAULT = 0.05        # transaction-cost floor in R
LOOKBACK_DAYS = 120


async def _sample_r(db, user_id: str, scope: str | None,
                    symbol: str | None) -> list:
    from outcome_attribution import result_r
    since = (datetime.now(timezone.utc)
             - timedelta(days=LOOKBACK_DAYS)).isoformat()
    base_q = {"user_id": user_id, "status": "closed",
              "closed_at": {"$gte": since},
              "alpha_clean": {"$ne": False}}
    for q in ({**base_q, "scope": scope, "symbol": symbol},
              {**base_q, "scope": scope}, base_q):
        q = {k: v for k, v in q.items() if v is not None}
        rs = []
        async for t in db.trades.find(
                q, {"pnl": 1, "entry_price": 1, "stop_loss": 1,
                    "exit_price": 1, "action": 1}).sort(
                "closed_at", -1).limit(300):
            r, _src = result_r(t)
            rs.append(r)
        if len(rs) >= 20:
            return rs
    return rs


def bootstrap_interval(rs: list, iters: int = BOOTSTRAP_ITERS,
                       seed: int | None = None) -> dict:
    rng = random.Random(seed)
    n = len(rs)
    means = sorted(sum(rng.choice(rs) for _ in range(n)) / n
                   for _ in range(iters))
    return {"expected_edge_r": round(sum(rs) / n, 3),
            "lower_r": round(means[int(0.10 * iters)], 3),
            "upper_r": round(means[int(0.90 * iters)], 3), "n": n}


async def assess(db, user_id: str, signal: dict,
                 cost_r: float = COST_R_DEFAULT) -> dict:
    """Returns {expected_edge_r, interval, uncertainty (0-1), decision,
    hard_gate} — hard_gate=True means SKIP is evidence-backed."""
    scope = str(signal.get("scope") or signal.get("origin") or "ai")
    rs = await _sample_r(db, user_id, scope, signal.get("symbol"))
    # model disagreement — raw confidence vs calibrated probability
    conf_p = float(signal.get("confidence") or 50) / 100.0
    cal = signal.get("calibrated_p_win") or {}
    cal_p = cal.get("p") or cal.get("p_win")
    disagreement = round(abs(conf_p - float(cal_p)), 3) \
        if cal_p is not None else None
    if len(rs) < 20:
        return {"decision": "ADVISORY", "hard_gate": False,
                "uncertainty": 0.5, "n": len(rs),
                "disagreement": disagreement,
                "note": f"insufficient comparable history "
                        f"({len(rs)} trades) — uncertainty engine advises "
                        f"only; other gates decide"}
    bi = bootstrap_interval(rs)
    width = bi["upper_r"] - bi["lower_r"]
    uncertainty = round(min(1.0, width / 2.0
                            + (disagreement or 0) * 0.5), 3)
    tradable = bi["lower_r"] > cost_r
    hard = (not tradable) and bi["n"] >= MIN_SAMPLES_HARD_GATE
    return {"expected_edge_r": bi["expected_edge_r"],
            "interval": [bi["lower_r"], bi["upper_r"]],
            "uncertainty": uncertainty, "n": bi["n"],
            "cost_r": cost_r, "disagreement": disagreement,
            "decision": "TRADE" if tradable else "SKIP",
            "hard_gate": hard,
            "note": (f"lower bound {bi['lower_r']}R "
                     f"{'clears' if tradable else 'does NOT clear'} "
                     f"costs {cost_r}R")}
