"""iter-114 · Advanced Risk Engine — a dedicated pre-trade risk authority.

Profitable models die from poor risk management. Before any order reaches
the broker, five checks run and produce one verdict {allow, scale, checks}:

  1. DYNAMIC LEVERAGE — max notional = equity × max_leverage, scaled DOWN
     by volatility expansion and UP/DOWN by calibrated confidence.
  2. EVENT EXPOSURE — around major macro prints (≤60min), correlated USD
     exposure is capped: new adds blocked above the cap, halved above 50%.
  3. DRAWDOWN LADDER — daily / weekly (existing breakers) re-checked here,
     plus a MONTHLY limit: breach = trading suspended for the month.
  4. ABNORMAL MARKET — volatility shocks (last bar ≫ median range) and
     stale data feeds suspend or halve trading for the cycle.
  5. CVaR BUDGET — projected portfolio expected-shortfall (95%) including
     the new position must fit the budget; oversize gets trimmed to fit,
     hard-blocked beyond 1.5× budget. Stop distance is NOT the risk metric —
     tail expectation is."""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DEF_MAX_LEVERAGE = 20.0
DEF_MONTHLY_DD_PCT = 12.0
DEF_CVAR_BUDGET_PCT = 8.0
DEF_EVENT_EXPOSURE_CAP_PCT = 100.0
SHOCK_RANGE_MULT = 4.0
STALE_FEED_MIN = 45


def _notional(symbol: str, lot: float, price: float) -> float:
    base = (symbol or "").upper()
    return float(lot) * float(price) * (100 if "XAU" in base else 1)


def leverage_check(equity, symbol, lot, price, bars, uncertainty,
                   max_leverage=DEF_MAX_LEVERAGE) -> dict:
    if equity <= 0 or not lot or not price:
        return {"name": "leverage", "status": "ok", "scale": 1.0,
                "detail": "no sizing context"}
    vol_adj = 1.0
    if bars and len(bars) >= 30:
        from adaptive_sizing import volatility_mult
        vm = volatility_mult(bars)              # <1 when vol expanded
        vol_adj = max(0.4, min(1.0, vm))
    conf = (uncertainty or {}).get("confidence_pct")
    conf_adj = 1.0
    if conf is not None:
        conf_adj = 0.6 if conf < 65 else (1.2 if conf > 85 else 1.0)
    cap = equity * max_leverage * vol_adj * conf_adj
    notional = _notional(symbol, lot, price)
    if notional <= cap:
        return {"name": "leverage", "status": "ok", "scale": 1.0,
                "detail": f"notional ${notional:,.0f} ≤ dynamic cap "
                          f"${cap:,.0f} ({max_leverage}× · vol {vol_adj} · "
                          f"conf {conf_adj})"}
    return {"name": "leverage", "status": "trim",
            "scale": round(cap / notional, 3),
            "detail": f"notional ${notional:,.0f} > dynamic cap ${cap:,.0f} "
                      f"— leverage trimmed to fit "
                      f"(vol adj {vol_adj}, conf adj {conf_adj})"}


def event_exposure_check(open_notional, new_notional, equity,
                         minutes_to_event, event_title=None,
                         cap_pct=DEF_EVENT_EXPOSURE_CAP_PCT) -> dict:
    if minutes_to_event is None or not (0 <= minutes_to_event <= 60) \
            or equity <= 0:
        return {"name": "event_exposure", "status": "ok", "scale": 1.0,
                "detail": "no major print inside 60min"}
    total_pct = 100.0 * (open_notional + new_notional) / equity
    if total_pct > cap_pct:
        return {"name": "event_exposure", "status": "block", "scale": 0.0,
                "detail": f"'{event_title}' in {minutes_to_event}min — "
                          f"correlated USD exposure would reach "
                          f"{total_pct:.0f}% of equity (cap {cap_pct:.0f}%). "
                          f"New adds blocked into the print."}
    if total_pct > cap_pct * 0.5:
        return {"name": "event_exposure", "status": "trim", "scale": 0.5,
                "detail": f"'{event_title}' in {minutes_to_event}min — "
                          f"exposure {total_pct:.0f}% of equity: new size "
                          f"halved into the print."}
    return {"name": "event_exposure", "status": "ok", "scale": 1.0,
            "detail": f"'{event_title}' in {minutes_to_event}min — exposure "
                      f"{total_pct:.0f}% within budget"}


def drawdown_check(pnl_day, pnl_week, pnl_month, equity,
                   daily_pct=3.0, weekly_pct=7.0,
                   monthly_pct=DEF_MONTHLY_DD_PCT) -> dict:
    if equity <= 0:
        return {"name": "drawdown", "status": "ok", "scale": 1.0,
                "detail": "no equity context"}
    for label, pnl, lim in (("daily", pnl_day, daily_pct),
                            ("weekly", pnl_week, weekly_pct),
                            ("monthly", pnl_month, monthly_pct)):
        dd_pct = -100.0 * pnl / equity if pnl < 0 else 0.0
        if lim and dd_pct >= lim:
            return {"name": "drawdown", "status": "block", "scale": 0.0,
                    "detail": f"{label} drawdown {dd_pct:.1f}% breaches the "
                              f"{lim:.0f}% limit (P&L ${pnl:,.0f}) — trading "
                              f"suspended for this {label} window."}
    worst = max((-100.0 * p / equity if p < 0 else 0.0)
                for p in (pnl_day, pnl_week, pnl_month))
    return {"name": "drawdown", "status": "ok", "scale": 1.0,
            "detail": f"worst window drawdown {worst:.1f}% — inside all "
                      f"limits (D{daily_pct}/W{weekly_pct}/M{monthly_pct}%)"}


def abnormal_market_check(bars, now_ts=None) -> dict:
    if not bars or len(bars) < 30:
        return {"name": "abnormal_market", "status": "ok", "scale": 1.0,
                "detail": "insufficient bars to judge"}
    import time as _t
    now_ts = now_ts or _t.time()
    ranges = sorted(b["h"] - b["l"] for b in bars)
    median = ranges[len(ranges) // 2] or 1e-9
    last = bars[-1]
    last_range = last["h"] - last["l"]
    dow = datetime.now(timezone.utc).weekday()
    age_min = (now_ts - float(last.get("t") or 0)) / 60.0
    if dow < 5 and age_min > STALE_FEED_MIN:
        return {"name": "abnormal_market", "status": "block", "scale": 0.0,
                "detail": f"candle feed stale ({age_min:.0f}min old) — "
                          f"flying blind, trading suspended until data "
                          f"resumes."}
    if last_range >= SHOCK_RANGE_MULT * median:
        return {"name": "abnormal_market", "status": "block", "scale": 0.0,
                "detail": f"volatility shock: last bar range "
                          f"{last_range / median:.1f}× the median — abnormal "
                          f"conditions, standing aside this cycle."}
    if last_range >= 2.5 * median:
        return {"name": "abnormal_market", "status": "trim", "scale": 0.5,
                "detail": f"elevated volatility ({last_range / median:.1f}× "
                          f"median bar) — size halved."}
    return {"name": "abnormal_market", "status": "ok", "scale": 1.0,
            "detail": f"conditions normal (last bar "
                      f"{last_range / median:.1f}× median)"}


async def cvar_budget_check(open_trades, new_position, equity,
                            budget_pct=DEF_CVAR_BUDGET_PCT) -> dict:
    from portfolio.var import calculate_var
    snap = await calculate_var(list(open_trades) + [new_position],
                               equity=equity)
    cvar_pct = float(snap.get("cvar_95_pct_equity") or 0)
    if cvar_pct <= budget_pct:
        return {"name": "cvar_budget", "status": "ok", "scale": 1.0,
                "detail": f"projected CVaR₉₅ {cvar_pct:.1f}% of equity "
                          f"(${snap.get('cvar_95_usd', 0):,.0f}) within the "
                          f"{budget_pct:.0f}% budget", "cvar_pct": cvar_pct}
    if cvar_pct > budget_pct * 1.5:
        return {"name": "cvar_budget", "status": "block", "scale": 0.0,
                "detail": f"projected CVaR₉₅ {cvar_pct:.1f}% > "
                          f"{budget_pct * 1.5:.0f}% hard ceiling — expected "
                          f"tail loss too large, trade blocked.",
                "cvar_pct": cvar_pct}
    return {"name": "cvar_budget", "status": "trim",
            "scale": round(budget_pct / cvar_pct, 3),
            "detail": f"projected CVaR₉₅ {cvar_pct:.1f}% over the "
                      f"{budget_pct:.0f}% budget — size trimmed to fit.",
            "cvar_pct": cvar_pct}


async def _period_pnls(db, user_id, account_id=None):
    now = datetime.now(timezone.utc)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = day0 - timedelta(days=now.weekday())
    month0 = day0.replace(day=1)
    q = {"user_id": user_id, "status": "closed", "pnl": {"$ne": None},
         "closed_at": {"$gte": month0.isoformat()}}
    if account_id:
        q["account_id"] = account_id
    trades = await db.trades.find(q, {"pnl": 1, "closed_at": 1}).to_list(5000)
    d = w = m = 0.0
    for t in trades:
        p = float(t["pnl"])
        m += p
        if t["closed_at"] >= week0.isoformat():
            w += p
        if t["closed_at"] >= day0.isoformat():
            d += p
    return d, w, m


async def risk_engine_status(db, user_id) -> dict | None:
    """Lightweight snapshot for the posture UI."""
    acc = await db.accounts.find_one({"user_id": user_id, "connected": True}) \
        or await db.accounts.find_one({"user_id": user_id})
    if not acc:
        return None
    equity = float(acc.get("equity") or acc.get("balance") or 0)
    d, w, m = await _period_pnls(db, user_id)
    dd = drawdown_check(d, w, m, equity)
    open_trades = await db.trades.find(
        {"user_id": user_id,
         "status": {"$in": ["open", "pending"]}}).to_list(50)
    cvar = None
    try:
        from portfolio.var import calculate_var
        snap = await calculate_var(open_trades, equity=equity)
        cvar = {"cvar_95_pct": snap.get("cvar_95_pct_equity"),
                "cvar_95_usd": snap.get("cvar_95_usd")}
    except Exception as e:  # noqa: BLE001
        logger.debug("risk status cvar failed: %s", e)
    return {"equity": round(equity, 2),
            "pnl_windows": {"day": round(d, 2), "week": round(w, 2),
                            "month": round(m, 2)},
            "drawdown": dd, "cvar": cvar,
            "open_positions": len(open_trades)}


async def risk_engine_evaluate(db, user_id, cfg, account, signal,
                               lot, account_id=None) -> dict:
    """Run all five checks. Returns {allow, scale, checks, blocked_by}."""
    from pip_utils import base_symbol
    equity = float(account.get("equity") or account.get("balance") or 0)
    sym = signal.get("symbol") or ""
    base = base_symbol(sym)
    price = float(signal.get("entry_price") or 0)
    checks = []

    d, w, m = await _period_pnls(db, user_id, account_id)
    checks.append(drawdown_check(
        d, w, m, equity,
        daily_pct=float(cfg.get("daily_drawdown_pct") or 3.0),
        weekly_pct=float(cfg.get("weekly_drawdown_pct") or 7.0),
        monthly_pct=float(cfg.get("monthly_drawdown_pct")
                          or DEF_MONTHLY_DD_PCT)))

    cdoc = await db.intraday_candles.find_one(
        {"user_id": user_id, "symbol": base}, {"bars": 1})
    bars = (cdoc or {}).get("bars") or []
    checks.append(abnormal_market_check(bars))

    checks.append(leverage_check(
        equity, sym, lot, price, bars, signal.get("uncertainty"),
        max_leverage=float(cfg.get("max_leverage") or DEF_MAX_LEVERAGE)))

    open_q = {"user_id": user_id, "status": {"$in": ["open", "pending"]}}
    if account_id:
        open_q["account_id"] = account_id
    open_trades = await db.trades.find(open_q).to_list(50)
    open_notional = sum(
        _notional(t.get("symbol"), t.get("lot_size") or 0,
                  t.get("entry_price") or 0) for t in open_trades)
    minutes_to = (signal.get("calendar_intel") or {}).get("minutes_to")
    checks.append(event_exposure_check(
        open_notional, _notional(sym, lot, price), equity, minutes_to,
        event_title=(signal.get("calendar_intel") or {}).get("title"),
        cap_pct=float(cfg.get("event_exposure_cap_pct")
                      or DEF_EVENT_EXPOSURE_CAP_PCT)))

    try:
        checks.append(await cvar_budget_check(
            open_trades,
            {"symbol": sym, "lot_size": lot, "entry_price": price,
             "status": "open"},
            equity,
            budget_pct=float(cfg.get("cvar_budget_pct")
                             or DEF_CVAR_BUDGET_PCT)))
    except Exception as e:  # noqa: BLE001
        logger.debug("cvar check skipped: %s", e)

    blocked = next((c for c in checks if c["status"] == "block"), None)
    scale = 1.0
    for c in checks:
        scale *= float(c.get("scale") or 1.0)
    return {"allow": blocked is None, "scale": round(scale, 3),
            "blocked_by": blocked, "checks": checks,
            "pnl_windows": {"day": round(d, 2), "week": round(w, 2),
                            "month": round(m, 2)}}
