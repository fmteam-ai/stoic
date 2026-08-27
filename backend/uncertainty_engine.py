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


def conformal_interval(rs: list, alpha: float = 0.1) -> dict:
    """Split-conformal predictive interval over comparable R history:
    nonconformity = |r − median|; the (1−α) quantile bounds the next
    trade's likely R range with distribution-free coverage."""
    s = sorted(rs)
    n = len(s)
    med = s[n // 2]
    scores = sorted(abs(r - med) for r in rs)
    k = min(n - 1, int((1 - alpha) * (n + 1)))
    q = scores[k]
    return {"median_r": round(med, 3), "q_width": round(q, 3),
            "interval": [round(med - q, 3), round(med + q, 3)],
            "alpha": alpha}


async def assess(db, user_id: str, signal: dict,
                 cost_r: float = COST_R_DEFAULT,
                 market_state: dict | None = None) -> dict:
    """v60 Uncertainty 2.0 — bootstrap edge interval + model
    disagreement + conformal width + regime & execution uncertainty.
    Returns {expected_edge_r, interval, uncertainty (0-1), decision,
    hard_gate, components} — hard_gate=True means SKIP is evidence-backed."""
    scope = str(signal.get("scope") or signal.get("origin") or "ai")
    rs = await _sample_r(db, user_id, scope, signal.get("symbol"))
    # model disagreement — raw confidence vs calibrated probability
    conf_p = float(signal.get("confidence") or 50) / 100.0
    cal = signal.get("calibrated_p_win") or {}
    cal_p = cal.get("p") or cal.get("p_win")
    disagreement = round(abs(conf_p - float(cal_p)), 3) \
        if cal_p is not None else None
    ms = market_state or signal.get("market_state") or {}
    vec = ms.get("vector") or {}
    regime_u = 0.15 if ms.get("available") is False or not vec else 0.0
    try:
        exec_u = round(min(0.1, float(vec.get("spread_stress") or 0)
                           * 0.1), 3)
    except (TypeError, ValueError):
        exec_u = 0.0
    if len(rs) < 20:
        return {"decision": "ADVISORY", "hard_gate": False,
                "uncertainty": 0.5, "n": len(rs),
                "disagreement": disagreement,
                "components": {"regime_uncertainty": regime_u,
                               "execution_uncertainty": exec_u},
                "note": f"insufficient comparable history "
                        f"({len(rs)} trades) — uncertainty engine advises "
                        f"only; other gates decide"}
    bi = bootstrap_interval(rs)
    ci = conformal_interval(rs)
    width = bi["upper_r"] - bi["lower_r"]
    conformal_u = round(min(0.2, ci["q_width"] / 10.0), 3)
    components = {"bootstrap_width": round(width, 3),
                  "disagreement": disagreement or 0.0,
                  "conformal_uncertainty": conformal_u,
                  "regime_uncertainty": regime_u,
                  "execution_uncertainty": exec_u}
    uncertainty = round(min(1.0, width / 2.0
                            + (disagreement or 0) * 0.5
                            + conformal_u + regime_u + exec_u), 3)
    tradable = bi["lower_r"] > cost_r
    hard = (not tradable) and bi["n"] >= MIN_SAMPLES_HARD_GATE
    return {"expected_edge_r": bi["expected_edge_r"],
            "interval": [bi["lower_r"], bi["upper_r"]],
            "conformal": ci,
            "uncertainty": uncertainty, "n": bi["n"],
            "cost_r": cost_r, "disagreement": disagreement,
            "components": components,
            "decision": "TRADE" if tradable else "SKIP",
            "hard_gate": hard,
            "note": (f"lower bound {bi['lower_r']}R "
                     f"{'clears' if tradable else 'does NOT clear'} "
                     f"costs {cost_r}R")}
