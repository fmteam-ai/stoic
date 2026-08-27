"""Pre-Trade Digital Twin (v60 #8) — simulate the trade BEFORE capital
commits. FAST path (pure checks, sub-20ms) for every trade; DEEP path
(seeded Monte Carlo, ~100-500ms budget) for swing/longer-term scopes.
The twin only ever REDUCES or REJECTS risk — it never increases it."""
import logging
import random
from datetime import datetime, timezone

logger = logging.getLogger("pretrade.twin")

GAP_MULT = 2.0
GAP_LOSS_CAP_PCT = 2.0     # gap-shock loss must fit 2% of equity
RISK_CAP_PCT = 5.0         # single-candidate stop risk hard cap
DEEP_MC_ITERS = 300
DEEP_ES_FLOOR_R = -0.6     # per-trade ES over the 5% worst 10-trade runs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fast_checks(*, risk_usd: float, equity: float, ev_r,
                required_edge_r, spread_stress) -> dict:
    """Pure fast-path: edge-vs-cost, gap shock, equity risk cap."""
    equity = max(float(equity or 0), 0.01)
    checks, fraction = [], 1.0
    if ev_r is not None and required_edge_r is not None \
            and float(ev_r) < float(required_edge_r):
        checks.append({"name": "edge_vs_cost", "passed": False,
                       "detail": f"conservative edge {ev_r}R < required "
                                 f"{required_edge_r}R (cost + margin)"})
    else:
        checks.append({"name": "edge_vs_cost", "passed": True,
                       "detail": "edge clears expected costs"
                       if ev_r is not None else "no calibrated edge — "
                       "other gates decide"})
    risk_cap = equity * RISK_CAP_PCT / 100.0
    if risk_usd > risk_cap:
        checks.append({"name": "equity_risk_cap", "passed": False,
                       "detail": f"stop risk ${risk_usd:.0f} > "
                                 f"{RISK_CAP_PCT}% of equity"})
    else:
        checks.append({"name": "equity_risk_cap", "passed": True,
                       "detail": f"stop risk ${risk_usd:.0f} within cap"})
    gap_loss = risk_usd * GAP_MULT
    gap_cap = equity * GAP_LOSS_CAP_PCT / 100.0
    if gap_loss > gap_cap:
        fraction = min(fraction, round(gap_cap / gap_loss, 3))
        checks.append({"name": "gap_shock", "passed": False,
                       "detail": f"{GAP_MULT}× gap-through-stop loss "
                                 f"${gap_loss:.0f} exceeds "
                                 f"{GAP_LOSS_CAP_PCT}% of equity — "
                                 f"reduce to {fraction}x"})
    else:
        checks.append({"name": "gap_shock", "passed": True,
                       "detail": f"gap-shock loss ${gap_loss:.0f} "
                                 f"within budget"})
    try:
        ss = float(spread_stress) if spread_stress is not None else 0.0
    except (TypeError, ValueError):
        ss = 0.0
    if ss >= 0.7:
        fraction = min(fraction, 0.7)
        checks.append({"name": "spread_stress", "passed": False,
                       "detail": f"spread stress {ss} — entry cost "
                                 f"abnormal, size reduced"})
    else:
        checks.append({"name": "spread_stress", "passed": True,
                       "detail": "spread regime normal"})
    hard_fail = any(not c["passed"] and c["name"] in
                    ("edge_vs_cost", "equity_risk_cap") for c in checks)
    return {"checks": checks, "fraction": fraction,
            "hard_fail": hard_fail}


def deep_mc(p_win, rr, iters: int = DEEP_MC_ITERS, seed: int = 7) -> dict:
    """Seeded Monte Carlo over a 10-trade forward sequence."""
    try:
        p = float(p_win)
        payoff = max(0.2, float(rr or 1.5))
    except (TypeError, ValueError):
        return {"available": False}
    rng = random.Random(seed)
    totals = []
    for _ in range(iters):
        totals.append(sum(payoff if rng.random() < p else -1.0
                          for _ in range(10)))
    totals.sort()
    k = max(1, int(0.05 * iters))
    es5 = sum(totals[:k]) / k / 10.0   # per-trade ES over the sequence
    return {"available": True, "iters": iters,
            "p_sequence_profit": round(
                sum(1 for t in totals if t > 0) / iters, 3),
            "es5_per_trade_r": round(es5, 2),
            "passed": es5 >= DEEP_ES_FLOOR_R}


async def simulate(db, user_id: str, signal: dict, lot: float,
                   equity: float) -> dict:
    """TRADE / REDUCE / SKIP with approved fraction. Fail-open on error
    (callers wrap) — but verdicts themselves are downscale-only."""
    t_start = datetime.now(timezone.utc)
    from portfolio_risk import position_risk_usd
    cand = {"symbol": signal.get("symbol"),
            "action": signal.get("action"), "lot": lot,
            "entry_price": signal.get("entry_price"),
            "stop_loss": signal.get("stop_loss")}
    risk_usd = position_risk_usd(cand)
    cal = signal.get("calibrated_p_win") or {}
    tc = (signal.get("meta_decision") or {}).get("transaction_cost") or {}
    vec = (signal.get("market_state") or {}).get("vector") or {}
    fast = fast_checks(risk_usd=risk_usd, equity=equity,
                       ev_r=cal.get("ev_r"),
                       required_edge_r=tc.get("required_edge_r"),
                       spread_stress=vec.get("spread_stress"))
    scope = str(signal.get("scope") or "").lower()
    mode = "fast"
    deep = None
    if "swing" in scope or signal.get("trend_ride"):
        mode = "deep"
        deep = deep_mc(cal.get("p") or cal.get("p_win"),
                       signal.get("rr_ratio"))
        if deep.get("available") and not deep.get("passed"):
            fast["fraction"] = min(fast["fraction"], 0.5)
            fast["checks"].append(
                {"name": "deep_monte_carlo", "passed": False,
                 "detail": f"10-trade ES(5%) "
                           f"{deep['es5_per_trade_r']}R/trade below "
                           f"{DEEP_ES_FLOOR_R}R floor — size halved"})
    if fast["hard_fail"]:
        verdict, fraction = "SKIP", 0.0
    elif fast["fraction"] < 1.0:
        verdict, fraction = "REDUCE", fast["fraction"]
    else:
        verdict, fraction = "TRADE", 1.0
    elapsed_ms = round((datetime.now(timezone.utc)
                        - t_start).total_seconds() * 1000, 1)
    out = {"verdict": verdict, "approved_fraction": fraction,
           "mode": mode, "checks": fast["checks"], "deep": deep,
           "risk_usd": round(risk_usd, 2), "elapsed_ms": elapsed_ms,
           "engine_version": 1, "at": _now()}
    try:
        await db.pretrade_twin.insert_one(
            {"user_id": user_id, "symbol": signal.get("symbol"),
             "scope": signal.get("scope"),
             "decision_id": signal.get("decision_id"), **out})
        out.pop("_id", None)
    except Exception as e:  # noqa: BLE001
        logger.debug("twin persist failed: %s", e)
    return out
