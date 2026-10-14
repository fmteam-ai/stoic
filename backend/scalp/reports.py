"""v1.60.32 — Exit Rule Scoreboard (C8 breakeven 0.5R / C9 time-stop 90m) and Shadow Expectancy Report.

Pure aggregation helpers over closed scalp-engine trades and scalp_decisions; nothing here mutates state.
"""
from datetime import datetime, timedelta, timezone

from pip_utils import pip_value_usd_per_lot, base_symbol
from strategy_engines import SCALP_ENGINES

_SCALP_FILTER = {"$or": [{"engine": {"$in": list(SCALP_ENGINES)}}, {"scope": {"$in": list(SCALP_ENGINES)}}]}


def planned_risk_usd(trade: dict) -> float | None:
    """Risk at the ORIGINAL stop: recorded risk_amount when present, else lots × sl_pips × pip value."""
    ra = trade.get("risk_amount")
    try:
        if ra is not None and float(ra) > 0:
            return float(ra)
    except (TypeError, ValueError):
        pass
    try:
        lots = float(trade.get("original_lot_size") or trade.get("lot_size") or 0)
        sl_pips = float(trade.get("sl_pips") or 0)
        pv = pip_value_usd_per_lot(trade.get("symbol"))
        if lots > 0 and sl_pips > 0 and pv:
            return round(lots * sl_pips * float(pv), 2)
    except (TypeError, ValueError):
        pass
    return None


def classify_exit(trade: dict) -> dict:
    """One closed scalp-engine trade → {rule, outcome, pnl, risk, avoided_loss}.

    rule     : breakeven_0_5r | time_stop_90m | none
    outcome  : be_stop (stopped at the breakeven stop) | target (TP after BE) | time_stop | other
    avoided  : the part of the ORIGINAL stop the rule spared — estimate: planned risk + realised pnl, only when the
               trade closed at or below breakeven (a trade that went on to TP spared nothing, it just rode on).
    """
    pnl = float(trade.get("pnl") or 0.0)
    risk = planned_risk_usd(trade)
    rule = trade.get("scalp_exit_rule") or ("time_stop_90m" if trade.get("close_reason") == "time_stop" else "none")
    cr = str(trade.get("close_reason") or "")
    if rule == "time_stop_90m" or cr == "time_stop":
        rule, outcome = "time_stop_90m", "time_stop"
    elif rule == "breakeven_0_5r":
        outcome = "target" if cr.startswith("take_profit") or pnl > 0 and not cr.startswith("stop") else "be_stop"
    else:
        outcome = "other"
    avoided = None
    if rule != "none" and risk is not None and pnl <= 0.0 + 1e-9:
        avoided = round(max(0.0, risk + pnl), 2)
    elif rule != "none" and risk is not None and outcome == "be_stop":
        avoided = round(max(0.0, risk), 2)          # closed at entry+costs: the whole planned loss was spared
    return {"rule": rule, "outcome": outcome, "pnl": round(pnl, 2), "risk": risk, "avoided_loss": avoided}


def summarise(trades: list[dict], days: int) -> dict:
    groups = {"breakeven_0_5r": {"trades": 0, "be_stop": 0, "target": 0, "pnl_usd": 0.0, "avoided_loss_usd": 0.0},
              "time_stop_90m": {"trades": 0, "pnl_usd": 0.0, "avoided_loss_usd": 0.0, "closed_negative": 0, "closed_positive": 0}}
    unaffected = {"trades": 0, "pnl_usd": 0.0, "stop_loss": 0, "take_profit": 0}
    for t in trades:
        c = classify_exit(t)
        if c["rule"] == "breakeven_0_5r":
            g = groups["breakeven_0_5r"]
            g["trades"] += 1
            g[c["outcome"] if c["outcome"] in ("be_stop", "target") else "be_stop"] += 1
            g["pnl_usd"] += c["pnl"]
            g["avoided_loss_usd"] += c["avoided_loss"] or 0.0
        elif c["rule"] == "time_stop_90m":
            g = groups["time_stop_90m"]
            g["trades"] += 1
            g["closed_negative" if c["pnl"] < 0 else "closed_positive"] += 1
            g["pnl_usd"] += c["pnl"]
            g["avoided_loss_usd"] += c["avoided_loss"] or 0.0
        else:
            unaffected["trades"] += 1
            unaffected["pnl_usd"] += c["pnl"]
            cr = str(t.get("close_reason") or "")
            if cr.startswith("stop"):
                unaffected["stop_loss"] += 1
            elif cr.startswith("take_profit"):
                unaffected["take_profit"] += 1
    for g in list(groups.values()) + [unaffected]:
        for k in ("pnl_usd", "avoided_loss_usd"):
            if k in g:
                g[k] = round(g[k], 2)
    total_avoided = round(groups["breakeven_0_5r"]["avoided_loss_usd"] + groups["time_stop_90m"]["avoided_loss_usd"], 2)
    fired = groups["breakeven_0_5r"]["trades"] + groups["time_stop_90m"]["trades"]
    if not fired:
        lesson = "Neither exit rule has fired on a closed scalp trade yet — the scoreboard fills in as trades close."
    elif total_avoided > 0:
        lesson = (f"The two rules fired on {fired} trade(s) and spared an estimated ${total_avoided:,.2f} of planned stop "
                  "loss. Opportunity cost (targets the stop-out would have reached) is not simulated here.")
    else:
        lesson = f"The two rules fired on {fired} trade(s) with no measurable spared loss so far."
    return {"days": days, "rules": groups, "unaffected": unaffected, "fired": fired,
            "avoided_loss_usd": total_avoided, "lesson": lesson,
            "note": "avoided loss = planned risk at the original stop + realised P&L, counted only on trades that closed "
                    "at or below breakeven; an estimate, not a counterfactual price simulation"}


async def exit_rule_scoreboard(db, user_id: str, days: int = 30) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = {"user_id": user_id, "status": "closed", "closed_at": {"$gte": cutoff}, **_SCALP_FILTER}
    rows = await db.trades.find(q, {"pnl": 1, "risk_amount": 1, "lot_size": 1, "original_lot_size": 1, "sl_pips": 1,
                                    "symbol": 1, "close_reason": 1, "scalp_exit_rule": 1, "engine": 1, "scope": 1}).to_list(5000)
    return summarise(rows, days)


# ── Shadow Expectancy Report ──────────────────────────────────────────────────────────────────────────────────────
async def mode_stats(db, user_id: str, symbol: str, mode: str, setup: str | None = "range_break_momentum_v2") -> dict:
    """Labeled outcomes for one execution mode ("shadow" | "demo_live") → n, net, expectancy, pf, win rate."""
    q = {"user_id": user_id, "symbol": symbol, "outcome.result": {"$exists": True}}
    q["execution_mode"] = {"$in": ["shadow", None]} if mode == "shadow" else mode
    if setup:
        q["setup.setup"] = setup
    n = wins = 0
    net = gw = gl = 0.0
    async for d in db.scalp_decisions.find(q, {"outcome": 1}):
        v = float((d.get("outcome") or {}).get("net_pips") or 0.0)
        n += 1
        net += v
        if v > 0:
            wins += 1
            gw += v
        else:
            gl += v
    pf = (gw / abs(gl)) if gl < 0 else None
    return {"mode": mode, "labeled": n, "net_pips": round(net, 2), "expectancy_pips": round(net / n, 3) if n else None,
            "profit_factor": round(pf, 3) if pf is not None else None, "win_rate": round(wins / n, 3) if n else None}


async def shadow_expectancy_report(db, user_id: str, symbols: list[str]) -> dict:
    from scalp.live_gate import live_eligibility
    out = []
    for sym in symbols:
        sym = base_symbol(sym) or sym.upper()
        gate = await live_eligibility(db, user_id, sym)
        out.append({"symbol": sym, "gate": gate,
                    "shadow": await mode_stats(db, user_id, sym, "shadow"),
                    "live": await mode_stats(db, user_id, sym, "demo_live")})
    return {"symbols": out}
