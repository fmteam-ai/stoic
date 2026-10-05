from datetime import datetime, timezone, timedelta
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from bson import ObjectId
from pydantic import BaseModel, Field

from auth import get_current_user
from database import get_db
from market import get_quote
from rate_limiter import check_and_record
from execution import for_account as engine_for_account
from route_utils import parse_object_id
from trade_reconciler import reconcile_user
from risk import get_profile, compute_lot_for_account

router = APIRouter(prefix="/trades", tags=["trades"])


@router.get("/events")
async def trade_lifecycle_events(trade_id: Optional[str] = None,
                                 decision_id: Optional[str] = None,
                                 limit: int = 200,
                                 user=Depends(get_current_user)):
    """Chronological immutable lifecycle events for one trade or decision
    (DecisionCreated → ... → FinancialApplied)."""
    if not trade_id and not decision_id:
        raise HTTPException(status_code=422,
                            detail="trade_id or decision_id is required")
    db = get_db()
    q = {"user_id": user["id"]}
    if trade_id:
        q["trade_id"] = trade_id
    if decision_id:
        q["decision_id"] = decision_id
    out = []
    async for ev in db.trade_events.find(q, {"_id": 0}).sort(
            "ts_ms", 1).limit(min(max(limit, 1), 500)):
        out.append(ev)
    return {"events": out, "count": len(out)}


@router.post("/reconcile")
async def reconcile_open_trades(force: bool = False, user=Depends(get_current_user)):
    """Force-close any DB-open trade that the broker no longer reports as open.

    Uses the `open_tickets` list stored on each account from the last heartbeat
    (EA v1.22+). Returns a per-account summary of how many trades were closed.
    Frontend exposes this via the SYNC WITH BROKER button on the Trades page.

    Pass `?force=true` when the user has manually verified on MT5 that positions
    are closed but the EA is offline (so the standard heartbeat-based safety
    guards would otherwise refuse to close anything).
    """
    return await reconcile_user(user["id"], force=force)


class ManualTradeRequest(BaseModel):
    account_id: str
    symbol: str
    action: Literal["BUY", "SELL"]
    lot_size: float = Field(0.01, gt=0, le=100)
    sl_pips: float = Field(150, gt=0, le=10000)
    tp1_pips: float = Field(100, gt=0, le=10000)
    tp2_pips: float = Field(200, gt=0, le=10000)
    tp3_pips: float = Field(300, gt=0, le=10000)


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    return doc


@router.get("")
async def list_trades(limit: int = 100, status: str = None,
                      account_id: Optional[str] = None,
                      user=Depends(get_current_user)):
    db = get_db()
    query = {"user_id": user["id"]}
    if status:
        query["status"] = status
    if account_id:
        query["account_id"] = account_id
    cursor = db.trades.find(query).sort("opened_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)
    return [_serialize(d) for d in docs]


STOIC_MAGIC = 901234


def _is_bot_trade(t: dict) -> bool:
    """Mirror of the frontend SOURCE_BADGE_FOR classification."""
    o = t.get("origin") or ""
    if o.startswith("auto"):
        return True
    if o.startswith("manual"):
        return False
    if t.get("signal_id"):
        return True
    return (t.get("magic_number") or 0) == STOIC_MAGIC


def _split_stats(closed: list) -> dict:
    """W/L + P&L aggregates split into bot vs manual buckets."""
    out = {}
    for key, rows in (("bot", [t for t in closed if _is_bot_trade(t)]),
                      ("manual", [t for t in closed if not _is_bot_trade(t)])):
        wins = [t for t in rows if (t.get("pnl") or 0) > 0]
        losses = [t for t in rows if (t.get("pnl") or 0) < 0]
        decided = len(wins) + len(losses)
        out[key] = {
            "total_trades": len(rows),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / decided * 100, 1) if decided else 0.0,
            "total_pnl": round(sum(float(t.get("pnl") or 0) for t in rows), 2),
        }
    return out


def _aggregate_stats(closed: list) -> dict:
    """Compute aggregate P&L stats from a list of closed trade docs."""
    total = len(closed)
    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    losses = [t for t in closed if (t.get("pnl") or 0) < 0]
    total_pnl = sum((t.get("pnl") or 0) for t in closed)
    win_rate = (len(wins) / total * 100) if total else 0
    avg_win = (sum(t["pnl"] for t in wins) / len(wins)) if wins else 0
    avg_loss = (sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
    return {
        "total_trades": total,
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "wins": len(wins),
        "losses": len(losses),
    }


@router.get("/scoreboard")
async def strategy_scoreboard(days: int = 30, user=Depends(get_current_user)):
    """iter-135 · Per-engine attribution (quant roadmap #9): W/L, profit
    factor, long/short split and version per engine persona, plus the
    decision-funnel gate counts from the ledger."""
    db = get_db()
    q = {"user_id": user["id"], "status": "closed", "origin": "auto",
         "pnl": {"$ne": None}}
    if days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q["closed_at"] = {"$gte": since}
    trades = await db.trades.find(q).to_list(3000)

    # Backfill engine scope from the originating signal for pre-iter-133 trades
    missing = [t["signal_id"] for t in trades if not t.get("scope") and t.get("signal_id")]
    sig_scope = {}
    if missing:
        oids = []
        for s in set(missing):
            try:
                oids.append(ObjectId(s))
            except Exception:
                pass
        async for s in db.signals.find({"_id": {"$in": oids}}, {"scope": 1}):
            sig_scope[str(s["_id"])] = s.get("scope")

    rows = {}
    for t in trades:
        scope = t.get("scope") or sig_scope.get(str(t.get("signal_id"))) or "unattributed"
        r = rows.setdefault(scope, {
            "engine": scope, "trades": 0, "wins": 0, "losses": 0,
            "pnl": 0.0, "gross_win": 0.0, "gross_loss": 0.0,
            "long_pnl": 0.0, "short_pnl": 0.0, "symbols": {}, "version": None,
            "_pnls": [], "slippage_sum": 0.0, "slippage_n": 0})
        pnl = float(t.get("pnl") or 0)
        r["_pnls"].append(pnl)
        if t.get("slippage_pips") is not None:
            r["slippage_sum"] += abs(float(t.get("slippage_pips") or 0))
            r["slippage_n"] += 1
        r["trades"] += 1
        r["pnl"] += pnl
        if pnl > 0:
            r["wins"] += 1
            r["gross_win"] += pnl
        elif pnl < 0:
            r["losses"] += 1
            r["gross_loss"] += -pnl
        if (t.get("action") or "").upper() == "BUY":
            r["long_pnl"] += pnl
        else:
            r["short_pnl"] += pnl
        sym = t.get("symbol") or "?"
        r["symbols"][sym] = round(r["symbols"].get(sym, 0.0) + pnl, 2)
        v = (t.get("versions") or {}).get("strategy_version")
        if v:
            r["version"] = v

    out = []
    for r in rows.values():
        decided = r["wins"] + r["losses"]
        r["win_rate"] = round(r["wins"] / decided * 100, 1) if decided else 0.0
        r["profit_factor"] = (round(r["gross_win"] / r["gross_loss"], 2)
                              if r["gross_loss"] > 0 else None)
        r["avg_win"] = round(r["gross_win"] / r["wins"], 2) if r["wins"] else 0.0
        r["avg_loss"] = round(r["gross_loss"] / r["losses"], 2) if r["losses"] else 0.0
        for k in ("pnl", "gross_win", "gross_loss", "long_pnl", "short_pnl"):
            r[k] = round(r[k], 2)
        _sn = r.pop("slippage_n", 0)
        _ss = r.pop("slippage_sum", 0.0)
        r["avg_slippage_pips"] = round(_ss / _sn, 2) if _sn else None
        r.update(_risk_metrics(r.pop("_pnls", [])))
        out.append(r)
    out.sort(key=lambda x: -x["pnl"])

    # Portfolio-level metrics + calibration honesty (iter-137)
    all_pnls = [float(t.get("pnl") or 0) for t in
                sorted(trades, key=lambda x: x.get("closed_at") or "")]
    portfolio = _risk_metrics(all_pnls)
    gw = sum(p for p in all_pnls if p > 0)
    gl = sum(-p for p in all_pnls if p < 0)
    portfolio["profit_factor"] = round(gw / gl, 2) if gl > 0 else None
    slips = [abs(float(t.get("slippage_pips") or 0)) for t in trades
             if t.get("slippage_pips") is not None]
    portfolio["avg_slippage_pips"] = round(sum(slips) / len(slips), 2) if slips else None
    try:
        from calibration import compute_calibration
        cal = await compute_calibration(db, user["id"], days if days > 0 else 90)
        ns = sum(e["n"] for e in cal.values())
        portfolio["brier"] = (round(sum(e["brier"] * e["n"] for e in cal.values()) / ns, 4)
                              if ns else None)
        for r in out:
            r["brier"] = (cal.get(r["engine"]) or {}).get("brier")
    except Exception:
        portfolio["brier"] = None

    # Decision funnel from the permanent ledger
    dq = {"user_id": user["id"]}
    if days > 0:
        dq["ts"] = {"$gte": since}
    pipeline = [{"$match": dq},
                {"$group": {"_id": {"stage": "$stage", "status": "$status"},
                            "n": {"$sum": 1}}}]
    funnel = []
    async for g in db.trade_decisions.aggregate(pipeline):
        funnel.append({"stage": g["_id"]["stage"], "status": g["_id"]["status"],
                       "count": g["n"]})
    funnel.sort(key=lambda x: -x["count"])
    return {"days": days, "engines": out, "funnel": funnel,
            "portfolio": portfolio,
            "total_pnl": round(sum(r["pnl"] for r in out), 2)}


def _risk_metrics(pnls: list, window: int = 30) -> dict:
    """iter-137 · Rolling trade-level Sharpe/Sortino + max drawdown."""
    import statistics
    if not pnls:
        return {"sharpe": None, "sortino": None, "max_dd": 0.0}
    recent = pnls[-window:]
    mean = statistics.mean(recent)
    sd = statistics.pstdev(recent) if len(recent) > 1 else 0.0
    downside = [p for p in recent if p < 0]
    dsd = statistics.pstdev(downside) if len(downside) > 1 else (abs(downside[0]) if downside else 0.0)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {"sharpe": round(mean / sd, 2) if sd > 0 else None,
            "sortino": round(mean / dsd, 2) if dsd > 0 else None,
            "max_dd": round(max_dd, 2)}


@router.get("/calibration")
async def calibration_report(days: int = 90, user=Depends(get_current_user)):
    """iter-137 · Reliability table + Brier score per engine (roadmap item 1)."""
    from calibration import compute_calibration
    return {"days": days,
            "engines": await compute_calibration(get_db(), user["id"], days)}


@router.get("/ablation")
async def gate_ablation(days: int = 30, user=Depends(get_current_user)):
    """iter-136 · Gate ablation (quant roadmap #16): counterfactual replay of
    every vetoed setup — did each gate's rejections avoid losses or block
    winners?"""
    from ablation import run_gate_ablation
    db = get_db()
    return await run_gate_ablation(db, user["id"], days=days)


@router.get("/decisions")
async def list_trade_decisions(limit: int = 100, symbol: str = None,
                               status: str = None, stage: str = None,
                               user=Depends(get_current_user)):
    """iter-134 · Permanent decision-ledger audit trail (quant roadmap #2/#3).

    Every BUY/SELL candidate outcome — rejected (with gate + reason) or
    executed (with sizing + versions) — queryable forever.
    """
    db = get_db()
    q = {"user_id": user["id"]}
    if symbol:
        import re as _re
        q["symbol"] = {"$regex": f"^{_re.escape(symbol)}", "$options": "i"}
    if status:
        q["status"] = status
    if stage:
        q["stage"] = stage
    rows = await db.trade_decisions.find(q).sort("_id", -1).to_list(
        length=min(int(limit), 500))
    for r in rows:
        r["id"] = str(r.pop("_id"))
    pipeline = [{"$match": q}, {"$group": {"_id": "$stage", "n": {"$sum": 1}}},
                {"$sort": {"n": -1}}]
    stages = [{"stage": s["_id"], "count": s["n"]}
              for s in await db.trade_decisions.aggregate(pipeline).to_list(50)]
    return {"decisions": rows, "stage_counts": stages}


@router.get("/stats")
async def trade_stats(account_id: Optional[str] = None,
                      user=Depends(get_current_user)):
    db = get_db()
    closed_q = {"user_id": user["id"], "status": "closed"}
    open_q = {"user_id": user["id"], "status": "open"}
    if account_id:
        closed_q["account_id"] = account_id
        open_q["account_id"] = account_id
    # perf (prod outage) — pull ONLY the fields the aggregates below need.
    # Full documents at broker-history scale blew past the edge timeout
    # and the Trades page showed "temporarily unreachable".
    proj = {"pnl": 1, "origin": 1, "signal_id": 1,
            "magic_number": 1, "account_id": 1}
    closed = await db.trades.find(closed_q, proj).to_list(length=100000)
    open_count = await db.trades.count_documents(open_q)
    stats = {**_aggregate_stats(closed), **_split_stats(closed),
             "open_trades": open_count}
    # review P1-2 — the headline total must reconcile against the
    # per-account component sums from the SAME dataset; a number that
    # doesn't add up is flagged UNRECONCILED so the UI refuses to show it.
    components: dict = {}
    for t in closed:
        key = str(t.get("account_id") or "unassigned")
        components[key] = components.get(key, 0.0) + (t.get("pnl") or 0)
    components = {k: round(v, 2) for k, v in components.items()}
    sum_components = round(sum(components.values()), 2)
    delta = round(stats["total_pnl"] - sum_components, 2)
    tolerance = 0.01
    stats["reconciliation"] = {
        "total_pnl": stats["total_pnl"],
        "sum_components": sum_components,
        "components": components,
        "delta": delta,
        "tolerance": tolerance,
        "closed_trades": len(closed),
        "status": ("RECONCILED" if abs(delta) <= tolerance
                   else "UNRECONCILED"),
    }
    return stats


@router.get("/history")
async def trade_history(date_from: str, date_to: str,
                        account_id: Optional[str] = None,
                        user=Depends(get_current_user)):
    """Full trade history for an explicit date range (iter-44).

    Powers the period presets (today / yesterday / this week / last week /
    this month / last month) and the custom date search on the Trades page.
    Returns every matching trade (no 100-row cap) plus a summary computed
    over the CLOSED trades in the range: wins, losses, win rate, total P&L.

    Range matching: a trade belongs to the range if it CLOSED inside it;
    still-open/pending trades match on their open/created timestamp.
    Dates are yyyy-mm-dd, inclusive on both ends.
    """
    import re
    for d in (date_from, date_to):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d or ""):
            raise HTTPException(status_code=400, detail="Dates must be yyyy-mm-dd")
    if date_from > date_to:
        raise HTTPException(status_code=400, detail="date_from must be <= date_to")
    fr = f"{date_from}T00:00:00"
    to = f"{date_to}T23:59:59.999999"

    db = get_db()
    q: dict = {"user_id": user["id"], "$or": [
        {"closed_at": {"$gte": fr, "$lte": to}},
        {"$and": [
            {"$or": [{"closed_at": None}, {"closed_at": {"$exists": False}}]},
            {"$or": [
                {"opened_at": {"$gte": fr, "$lte": to}},
                {"$and": [
                    {"$or": [{"opened_at": None}, {"opened_at": {"$exists": False}}]},
                    {"created_at": {"$gte": fr, "$lte": to}},
                ]},
            ]},
        ]},
    ]}
    if account_id:
        q["account_id"] = account_id
    docs = await db.trades.find(q).sort([("closed_at", -1), ("opened_at", -1)]).to_list(length=5000)
    trades = [_serialize(d) for d in docs]

    closed = [t for t in trades if t.get("status") == "closed"]
    wins = [t for t in closed if float(t.get("pnl") or 0) > 0]
    losses = [t for t in closed if float(t.get("pnl") or 0) < 0]
    gross_profit = sum(float(t.get("pnl") or 0) for t in wins)
    gross_loss = abs(sum(float(t.get("pnl") or 0) for t in losses))
    summary = {
        "date_from": date_from,
        "date_to": date_to,
        "total_trades": len(trades),
        "closed_trades": len(closed),
        "open_trades": sum(1 for t in trades if t.get("status") == "open"),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(closed) - len(wins) - len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "total_pnl": round(gross_profit - gross_loss, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": round(gross_profit / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        **_split_stats(closed),
    }
    return {"summary": summary, "trades": trades}


@router.get("/live")
async def live_open_trades(account_id: Optional[str] = None,
                           user=Depends(get_current_user)):
    """Open trades enriched with current price, unrealised P&L, distance to SL/TP1/TP2/TP3.

    Used by the Dashboard's "Time-to-Target" widget. Refreshes every ~5s on the client.
    """
    from pip_utils import pip_size, price_to_pips
    db = get_db()
    query = {"user_id": user["id"], "status": {"$in": ["pending", "open"]}}
    if account_id:
        query["account_id"] = account_id
    cursor = db.trades.find(query).sort("opened_at", -1)
    trades = await cursor.to_list(length=50)
    if not trades:
        return []

    # Fetch latest quote per unique symbol once
    symbols = sorted({t["symbol"] for t in trades})
    prices = {}
    for sym in symbols:
        try:
            q = await get_quote(sym)
            prices[sym] = float(q.get("price") or 0)
        except Exception:
            prices[sym] = 0.0

    out = []
    for t in trades:
        sym = t["symbol"]
        action = t["action"]
        entry = float(t.get("entry_price") or 0)
        sl = float(t.get("stop_loss") or 0)
        tp1 = float(t.get("tp1") or 0)
        tp2 = float(t.get("tp2") or 0)
        tp3 = float(t.get("tp3") or t.get("take_profit") or 0)
        lot = float(t.get("lot_size") or 0)
        current = prices.get(sym, 0.0)

        def pip_diff_to_target(target):
            return price_to_pips(sym, abs(target - current)) if target and current else None
        pips_in_profit = None
        if current and entry:
            diff = (current - entry) if action == "BUY" else (entry - current)
            pips_in_profit = price_to_pips(sym, diff)

        # Unrealised P&L estimate (USD). For accuracy this should use contract size,
        # but for display we approximate: BTC pip = $1 per lot, gold pip = $1 per 0.01 lot.
        pip_val_per_lot = 10.0 if pip_size(sym) == 0.0001 else (
            1.0 if sym.upper() in ("BTCUSD", "ETHUSD") else 10.0
        )
        unrealised_pnl = None
        if pips_in_profit is not None:
            unrealised_pnl = round(pips_in_profit * pip_val_per_lot * lot, 2)

        # Distance to each target as percentage of total entry-to-target distance
        def _pct(target):
            if not (current and entry and target):
                return None
            full = abs(target - entry)
            covered = abs(current - entry) if (
                (action == "BUY" and current >= entry) or (action == "SELL" and current <= entry)
            ) else 0
            return round(min(100, (covered / full) * 100), 1) if full > 0 else None

        out.append({
            "id": str(t["_id"]),
            "account_id": t.get("account_id"),
            "symbol": sym,
            "action": action,
            "lot_size": lot,
            "entry_price": entry,
            "stop_loss": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "current_price": current,
            "pips_in_profit": round(pips_in_profit, 1) if pips_in_profit is not None else None,
            "unrealised_pnl": unrealised_pnl,
            "pips_to_sl": round(pip_diff_to_target(sl) or 0, 1) if sl else None,
            "pips_to_tp1": round(pip_diff_to_target(tp1) or 0, 1) if tp1 else None,
            "pips_to_tp2": round(pip_diff_to_target(tp2) or 0, 1) if tp2 else None,
            "pips_to_tp3": round(pip_diff_to_target(tp3) or 0, 1) if tp3 else None,
            "progress_to_tp1_pct": _pct(tp1),
            "progress_to_tp2_pct": _pct(tp2),
            "progress_to_tp3_pct": _pct(tp3),
            "tp1_closed": bool(t.get("tp1_closed")),
            "tp2_closed": bool(t.get("tp2_closed")),
            "tp3_closed": bool(t.get("tp3_closed")),
            "breakeven_set": bool(t.get("breakeven_set")),
            "status": t.get("status"),
            "mode": t.get("mode"),
            "opened_at": t.get("opened_at"),
        })
    return out


@router.post("/execute/{signal_id}")
async def execute_signal(signal_id: str, payload: dict, user=Depends(get_current_user)):
    """Queue a trade for execution via the MT5 EA bridge.

    Body: {"account_id": "..."}
    The trade is created in 'pending' status; the EA polls /bridge/poll-trades
    and reports back via /bridge/report.
    """
    account_id = payload.get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="account_id required")

    db = get_db()
    signal = await db.signals.find_one({"_id": parse_object_id(signal_id, "Signal"),
                                         "user_id": user["id"]})
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found")
    if signal.get("action") == "HOLD":
        raise HTTPException(status_code=400, detail="Cannot execute HOLD signal")

    account = await db.accounts.find_one({"_id": parse_object_id(account_id, "Account"),
                                           "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Kill-switch: hard cap on orders per minute per user
    rl = check_and_record(user["id"])
    if not rl["allowed"]:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {rl['limit']} orders/min reached. Retry in {rl['retry_in_s']}s.",
        )

    # Recompute lot size against the TARGET account's real equity (mirrors
    # bot_runner._process_user_account_locked) — the signal's lot was sized
    # against a hardcoded $1000 placeholder which over-shoots on small live
    # accounts and would otherwise be blocked by Safety Guardian's per-trade
    # risk cap. Honour any user-set `max_lot_size` cap on the matching bot_cfg.
    risk_level = signal.get("risk_level") or "medium"
    profile = get_profile(risk_level)
    sized = compute_lot_for_account(
        account=account,
        symbol=signal["symbol"],
        entry_price=signal.get("entry_price") or 0,
        stop_loss=signal.get("stop_loss") or 0,
        confidence_pct=float(signal.get("confidence") or 0),
        profile=profile,
    )
    # iter-144 C5 · fail-closed sizing on the manual execute path too
    if not sized.get("sizing_valid", True):
        raise HTTPException(
            status_code=422,
            detail=f"Sizing rejected: {sized.get('reject_reason') or sized.get('method')}")
    absolute_lot = float(sized.get("lot_size") or signal.get("lot_size", 0.01))
    # iter-142 · Implicit Kelly removed (quant review): the old branch scaled
    # the user's max_lot_size cap by the Kelly fraction even though Kelly
    # sizing is disabled by default — a zero fraction collapsed every
    # capped manual execute to a 0.01 dust lot. max_lot_size is now a plain
    # hard ceiling, identical to the bot runner's fixed-fraction path.
    bot_cfg = await db.bot_configs.find_one({
        "user_id": user["id"],
        "$or": [{"account_id": account_id}, {"account_id": None}],
    }) or {}
    max_lot_cap = float(bot_cfg.get("max_lot_size") or 0.0)
    effective_lot = min(absolute_lot, max_lot_cap) if max_lot_cap > 0 else absolute_lot

    # Fix plan B6 — executing an old signal by hand must pass the same live
    # price check as the bot: refuse when price moved > 50% of the stop
    # distance or already traded through the stop (no quote = refuse).
    entry0 = float(signal.get("entry_price") or 0)
    sl0 = float(signal.get("stop_loss") or 0)
    if entry0 > 0 and sl0 > 0:
        from market import get_quote
        try:
            px = float(((await get_quote(signal["symbol"])) or {}).get("price") or 0)
        except Exception:  # noqa: BLE001
            px = 0.0
        if px <= 0:
            raise HTTPException(status_code=409, detail={
                "code": "quote_unavailable",
                "message": "No live price for this symbol right now — try again in a moment."})
        stop_dist = abs(entry0 - sl0)
        deviation = abs(px - entry0)
        through_stop = ((signal["action"] == "BUY" and px <= sl0)
                        or (signal["action"] == "SELL" and px >= sl0))
        if through_stop or deviation > 0.5 * stop_dist:
            raise HTTPException(status_code=409, detail={
                "code": "entry_deviation",
                "message": (f"Price moved too far since this signal ({signal['symbol']} now {px:g}, "
                            f"signal entry {entry0:g}, stop {sl0:g}). Generate a fresh signal."),
                "live_price": px, "signal_entry": entry0, "deviation": round(deviation, 5)})

    # Execution Factory — paper vs live engine
    engine = engine_for_account(account)
    trade_doc = await engine.execute(
        user_id=user["id"],
        account=account,
        signal={
            "signal_id": signal_id,
            "symbol": signal["symbol"],
            "action": signal["action"],
            "lot_size": effective_lot,
            "entry_price": signal.get("entry_price", 0),
            "stop_loss": signal.get("stop_loss", 0),
            "take_profit": signal.get("take_profit", 0),
            "origin": "manual",
        },
        cfg_account_id=str(account["_id"]),   # fix plan B6: daily-loss / caps scoped to THIS account
    )
    await db.signals.update_one(
        {"_id": parse_object_id(signal_id, "Signal"), "user_id": user["id"]},
        {"$set": {"consumed": True}},
    )
    return trade_doc


@router.post("/manual")
async def execute_manual_trade(payload: ManualTradeRequest,
                               request: Request,
                               user=Depends(get_current_user)):
    """Place a manual paper trade — bypasses AI signal/confidence gating.

    Allowed ONLY for paper-mode accounts; live accounts must execute via AI signals
    so that the EA bridge + risk vetoes apply.

    v62.4 — if the account is the master of a strategy-GOVERNED PAMM
    program, this becomes the DEDICATED manual-override path: step-up MFA
    required, explicit override origin, and the trade is NEVER attributed
    to the assigned strategy."""
    db = get_db()
    account = await db.accounts.find_one({"_id": parse_object_id(payload.account_id, "Account"),
                                           "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if (account.get("mode") or "live").lower() != "paper":
        raise HTTPException(status_code=400, detail="Manual trades are only allowed on paper accounts")

    _pamm_override = False
    from modules.pamm.strategy_guard import governance_mode, resolve_program
    _prog = await resolve_program(db, str(account["_id"]), {})
    if _prog is not None and await governance_mode(db, _prog) == "STRATEGY":
        from step_up import audit_event, require_step_up
        await require_step_up(db, user, request, "risk_raise")
        await audit_event(db, user["id"], "pamm_manual_override",
                          {"program_id": _prog.get("program_id"),
                           "account_id": str(account["_id"]),
                           "symbol": payload.symbol,
                           "action": payload.action}, request,
                          step_up=True)
        _pamm_override = True

    rl = check_and_record(user["id"])
    if not rl["allowed"]:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: max {rl['limit']} orders/min reached. Retry in {rl['retry_in_s']}s.",
        )

    quote = await get_quote(payload.symbol)
    price = quote.get("price")
    if not price or price <= 0:
        raise HTTPException(status_code=502, detail=f"Could not get live quote for {payload.symbol}")

    from pip_utils import pips_to_price
    sl_dist = pips_to_price(payload.symbol, payload.sl_pips)
    tp1_dist = pips_to_price(payload.symbol, payload.tp1_pips)
    tp2_dist = pips_to_price(payload.symbol, payload.tp2_pips)
    tp3_dist = pips_to_price(payload.symbol, payload.tp3_pips)
    if payload.action == "BUY":
        stop_loss = round(price - sl_dist, 5)
        tp1 = round(price + tp1_dist, 5)
        tp2 = round(price + tp2_dist, 5)
        tp3 = round(price + tp3_dist, 5)
    else:
        stop_loss = round(price + sl_dist, 5)
        tp1 = round(price - tp1_dist, 5)
        tp2 = round(price - tp2_dist, 5)
        tp3 = round(price - tp3_dist, 5)

    engine = engine_for_account(account)
    trade_doc = await engine.execute(
        user_id=user["id"],
        account=account,
        signal={
            "signal_id": None,
            "symbol": payload.symbol,
            "action": payload.action,
            "lot_size": payload.lot_size,
            "entry_price": price,
            "stop_loss": stop_loss,
            "take_profit": tp3,
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3,
            "sl_pips": payload.sl_pips,
            "tp_pips": [payload.tp1_pips, payload.tp2_pips, payload.tp3_pips],
            "origin": "manual_override" if _pamm_override else "manual_test",
            "pamm_manual_override": _pamm_override,
        },
    )
    return trade_doc


@router.get("/{trade_id}/explain")
async def trade_explain(trade_id: str, user=Depends(get_current_user)):
    """Explainable AI breakdown for a trade.

    4 sections: WHY DID I ENTER, WHY THIS SIZE, WHAT FACTORS MATTERED, RISKS.
    Prefers the snapshot embedded on the trade doc at creation (stable across
    agent_activity rotation); falls back to live composition for older trades.
    """
    from trade_explainer import explain_trade
    db = get_db()
    trade = await db.trades.find_one(
        {"_id": parse_object_id(trade_id, "Trade"), "user_id": user["id"]}
    )
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return await explain_trade(db, trade)


@router.get("/{trade_id}/dna")
async def trade_dna(trade_id: str, user=Depends(get_current_user)):
    """Tier 1 — Decision DNA: consolidated, HMAC-signed decision record
    answering why / evidence / risks / outcome-vs-expectation."""
    from decision_dna import compose_dna
    db = get_db()
    trade = await db.trades.find_one(
        {"_id": parse_object_id(trade_id, "Trade"), "user_id": user["id"]})
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return await compose_dna(db, trade)


@router.get("/{trade_id}/trace")
async def trade_trace(trade_id: str, user=Depends(get_current_user)):
    """Phase 6 — full execution trace. Answers, with recorded evidence:
    why opened · why at that time · why that size · why that stop ·
    why that target · what changed in flight · why closed."""
    from execution_trace import compose
    db = get_db()
    trade = await db.trades.find_one(
        {"_id": parse_object_id(trade_id, "Trade"), "user_id": user["id"]}
    )
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return await compose(db, trade)


@router.get("/{trade_id}/replay")
async def trade_replay_route(trade_id: str, user=Depends(get_current_user)):
    """Phase 7 — tick-by-tick replay data for the trade's lifetime window."""
    from operator_tools import trade_replay
    db = get_db()
    trade = await db.trades.find_one(
        {"_id": parse_object_id(trade_id, "Trade"), "user_id": user["id"]})
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    return await trade_replay(db, trade)


@router.get("/{trade_id}/timeline")
async def trade_timeline_route(trade_id: str, user=Depends(get_current_user)):
    """iter-160 — full order-lifecycle audit timeline
    (signal→validation→risk→execution→confirmation→monitoring→close).
    Owners see their own trades; admins may audit any trade (ops support)."""
    from trade_timeline import assemble_timeline
    db = get_db()
    tl = await assemble_timeline(db, trade_id)
    if not tl:
        raise HTTPException(status_code=404, detail="Trade not found")
    if user.get("role") != "admin" and tl.get("user_id") != str(user["id"]):
        raise HTTPException(status_code=403, detail="Not your trade")
    return tl


class WhatIfIn(BaseModel):
    days: int = Field(default=30, ge=1, le=180)
    risk_pct: float | None = Field(default=None, gt=0, le=10)
    sl_mult: float | None = Field(default=None, gt=0.1, le=5)
    tp_mult: float | None = Field(default=None, gt=0.1, le=5)
    trailing_start_r: float | None = Field(default=None, gt=0.1, le=10)


@router.post("/what-if")
async def what_if_route(payload: WhatIfIn, user=Depends(get_current_user)):
    """Phase 7 — what-if analysis over the user's actual closed trades."""
    if not payload.model_dump(exclude={"days"}, exclude_none=True):
        raise HTTPException(status_code=422,
                            detail="provide at least one scenario parameter")
    from operator_tools import what_if
    db = get_db()
    return await what_if(db, user["id"], days=payload.days,
                         risk_pct=payload.risk_pct, sl_mult=payload.sl_mult,
                         tp_mult=payload.tp_mult,
                         trailing_start_r=payload.trailing_start_r)


@router.get("/{trade_id}/audit")
async def trade_audit(trade_id: str, user=Depends(get_current_user)):
    """Full deal lineage for a single trade.

    Joins the trade's own lifecycle events (created, opened, modified, closed)
    with every broker_deal that touched its mt5_ticket. Ordered chronologically.
    Powers the "AUDIT TRAIL" modal on the Trades page so the user can see
    when partial fills, SL/TP modifications, and the final close actually
    hit at the broker — with the exact broker timestamps.
    """
    db = get_db()
    trade = await db.trades.find_one(
        {"_id": parse_object_id(trade_id, "Trade"), "user_id": user["id"]}
    )
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    events: list[dict] = []

    # 1. STOIC lifecycle events from the trade doc itself
    if trade.get("opened_at"):
        events.append({
            "kind": "stoic_open",
            "at": trade["opened_at"],
            "label": "Trade created in STOIC",
            "details": {
                "symbol": trade.get("symbol"),
                "action": trade.get("action"),
                "lots": trade.get("lot_size"),
                "entry_price": trade.get("entry_price"),
                "stop_loss": trade.get("stop_loss"),
                "take_profit": trade.get("take_profit"),
                "origin": trade.get("origin"),
            },
        })
    if trade.get("breakeven_set"):
        events.append({
            "kind": "breakeven",
            "at": trade.get("breakeven_at") or trade.get("opened_at"),
            "label": "SL moved to break-even",
            "details": {"stop_loss": trade.get("stop_loss")},
        })
    if trade.get("partial_closed"):
        events.append({
            "kind": "partial_close",
            "at": trade.get("partial_closed_at") or trade.get("opened_at"),
            "label": "Partial close at TP1",
        })
    if trade.get("trail_active"):
        events.append({
            "kind": "trail_active",
            "at": trade.get("trail_started_at") or trade.get("opened_at"),
            "label": "Trailing stop activated",
        })
    if trade.get("closed_at"):
        events.append({
            "kind": "stoic_close",
            "at": trade["closed_at"],
            "label": f"STOIC marked closed ({trade.get('close_reason') or 'unknown'})",
            "details": {
                "exit_price": trade.get("exit_price"),
                "pnl": trade.get("pnl"),
                "close_reason": trade.get("close_reason"),
            },
        })
    if trade.get("revived_at"):
        events.append({
            "kind": "revived",
            "at": trade["revived_at"],
            "label": "Trade revived (re-opened from wrongful close)",
            "details": {
                "revived_from_close_reason": trade.get("revived_from_close_reason"),
                "revived_via_snapshot": trade.get("revived_via_snapshot"),
            },
        })

    # Pending EA modification — fires when STOIC has queued a change (partial
    # close, BE, FULL_CLOSE) that the EA hasn't acked yet. Without this, the
    # user sees a stuck trade with no visible reason in the audit log.
    pm = trade.get("pending_modification")
    if pm and isinstance(pm, dict):
        kind = pm.get("type") or "unknown"
        ts = pm.get("requested_at") or trade.get("opened_at")
        if kind == "PARTIAL_CLOSE":
            lbl = f"⏳ Pending EA action · partial close to {pm.get('new_volume')} lots"
            if pm.get("new_sl") is not None:
                lbl += f" + SL→{pm.get('new_sl')} (break-even)"
        elif kind == "MODIFY_SL":
            lbl = f"⏳ Pending EA action · SL→{pm.get('new_sl')}"
        elif kind == "FULL_CLOSE":
            lbl = "⏳ Pending EA action · full close requested"
        else:
            lbl = f"⏳ Pending EA action · {kind}"
        events.append({
            "kind": "pending_modification",
            "at": ts,
            "label": lbl,
            "details": pm,
        })
    if trade.get("last_modification_error"):
        events.append({
            "kind": "modification_error",
            "at": trade.get("opened_at"),
            "label": f"⚠ EA reported error: {trade.get('last_modification_error')}",
        })

    # 2. Broker deals — every MT5 event that touched this ticket
    ticket = trade.get("mt5_ticket")
    broker_deals: list[dict] = []
    if ticket:
        cursor = db.broker_deals.find(
            {"user_id": user["id"], "mt5_ticket": int(ticket)}
        ).sort("deal_time", 1)
        broker_deals = await cursor.to_list(length=500)

    for d in broker_deals:
        # Prefer broker-native timestamp; fall back to received_at if missing.
        deal_ts = d.get("deal_time")
        ts_iso = (
            datetime.fromtimestamp(deal_ts, tz=timezone.utc).isoformat()
            if deal_ts else d.get("received_at")
        )
        side = "BUY" if d.get("action") == "BUY" else "SELL"
        if d.get("deal_entry") == "in":
            lbl = f"Broker: position opened ({side} {d.get('lots')} @ {d.get('price')})"
        elif d.get("deal_entry") == "out":
            realized = (d.get("profit") or 0) + (d.get("commission") or 0) + (d.get("swap") or 0)
            sign = "+" if realized >= 0 else "-"
            lbl = f"Broker: position closed @ {d.get('price')} · P&L {sign}${abs(realized):.2f}"
        else:
            lbl = f"Broker: position reversed @ {d.get('price')}"
        events.append({
            "kind": "broker_deal",
            "at": ts_iso,
            "label": lbl,
            "details": {
                "deal_id": d.get("deal_id"),
                "deal_entry": d.get("deal_entry"),
                "price": d.get("price"),
                "lots": d.get("lots"),
                "profit": d.get("profit"),
                "commission": d.get("commission"),
                "swap": d.get("swap"),
                "magic": d.get("magic"),
                "is_manual": d.get("magic") == 0,
            },
        })

    # Sort chronologically. Some events may have None timestamps — push them last.
    def _sort_key(e):
        ts = e.get("at")
        return (1, "") if not ts else (0, ts)
    events.sort(key=_sort_key)

    # iter-154 · execution summary — quality scores, expected-vs-actual
    # slippage, commission/swap, realized R, MFE/MAE, reconciliation truth.
    ev = await db.trade_evaluations.find_one({"trade_id": trade_id})
    commission = sum(float(d.get("commission") or 0) for d in broker_deals)
    swap = sum(float(d.get("swap") or 0) for d in broker_deals)
    fin_status = None
    for d in reversed(broker_deals):
        if d.get("financial_reconciliation_status"):
            fin_status = d["financial_reconciliation_status"]
            break
    expected_cost_pips = None
    try:
        from monte_carlo import typical_cost
        from pip_utils import base_symbol as _bsym, price_to_pips
        if trade.get("entry_price"):
            _sym = trade.get("base_symbol") or _bsym(trade.get("symbol"))
            expected_cost_pips = round(price_to_pips(
                _sym, typical_cost(_sym, float(trade["entry_price"]))), 2)
    except Exception:
        pass
    realized_r = (ev or {}).get("realized_r")
    if realized_r is None and trade.get("pnl") is not None \
            and trade.get("risk_amount"):
        try:
            realized_r = round(float(trade["pnl"]) / float(trade["risk_amount"]), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    execution_summary = {
        "entry_quality": (ev or {}).get("entry_quality"),
        "exit_quality": (ev or {}).get("exit_quality"),
        "mfe_r": (ev or {}).get("mfe_r"),
        "mae_r": (ev or {}).get("mae_r"),
        "realized_r": realized_r,
        "lesson": (ev or {}).get("lesson"),
        "slippage_pips": trade.get("slippage_pips"),
        "expected_cost_pips": expected_cost_pips,
        "commission": round(commission, 2) if broker_deals else None,
        "swap": round(swap, 2) if broker_deals else None,
        "broker_error": (trade.get("error")
                         or trade.get("last_modification_error")),
        "reconciliation": {
            "financial_status": fin_status,
            "pnl_estimated": bool(trade.get("pnl_estimated")),
            "pnl_unknown": bool(trade.get("pnl_unknown")),
            "backfilled_at": trade.get("backfilled_at"),
            "replayed_at": trade.get("journal_replayed_at"),
        },
    }

    return {
        "trade_id": trade_id,
        "mt5_ticket": ticket,
        "events": events,
        "broker_deal_count": len(broker_deals),
        "execution_summary": execution_summary,
    }


class TradeBackfillExit(BaseModel):
    """DEPRECATED — kept only for the admin/maintenance endpoint below.

    Manual backfill was removed from the public UI: STOIC is full-autopilot.
    Trades that go ghost (closed via a non-EA terminal) are now caught by the
    EA v1.26 history sweep instead of asking the user to paste numbers.
    """
    exit_price: float = Field(..., gt=0)
    pnl: float
    closed_at: Optional[str] = None
    lot_size: Optional[float] = Field(None, gt=0)


@router.post("/{trade_id}/revive")
async def revive_trade(trade_id: str, user=Depends(get_current_user)):
    """Re-open a trade STOIC mistakenly marked closed.

    Use case: PANIC or the reconciler closed a trade in DB before the broker
    actually closed it (e.g. heartbeat blip + stale `open_tickets`). The
    position is still alive at MT5 and needs to come back under bot control
    (break-even, trailing, partial-close, PANIC).

    Refuses on anything other than `closed` + `exit_price=None` — we never
    want to "revive" a genuinely-closed trade and lose its realised P&L.
    """
    db = get_db()
    trade = await db.trades.find_one(
        {"_id": parse_object_id(trade_id, "Trade"), "user_id": user["id"]}
    )
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.get("status") != "closed":
        raise HTTPException(status_code=400, detail="Only closed trades can be revived")
    if trade.get("exit_price") is not None:
        raise HTTPException(
            status_code=400,
            detail="Trade has a recorded exit price — refusing to revive (would lose realised P&L).",
        )
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.trades.update_one(
        {"_id": parse_object_id(trade_id, "Trade")},
        {"$set": {
            "status": "open",
            "closed_at": None,
            "revived_at": now_iso,
            "revived_from_close_reason": trade.get("close_reason"),
            # Clear any pending EA modification (e.g. FULL_CLOSE queued by
            # slippage veto / reconciler) — otherwise the EA would close the
            # trade again on its next poll, undoing the revive.
            "pending_modification": None,
        },
         "$unset": {"close_reason": "", "reconciled": "", "close_requested": ""}},
    )
    return {"ok": True, "trade_id": trade_id, "status": "open"}


@router.post("/{trade_id}/close")
async def close_trade(trade_id: str, user=Depends(get_current_user)):
    """Request a broker-position close through the unified close protocol
    (r26 P2-01): the position stays OPEN, gains a durable close_seq + immutable
    command row, and the EA closes it on its next poll."""
    db = get_db()
    from close_commands import request_close
    out = await request_close(db, {"_id": parse_object_id(trade_id, "Trade"), "user_id": user["id"]},
                              reason="manual", actor=f"user:{user['id']}")
    if out["trades_marked_for_close"] == 0:
        raise HTTPException(status_code=404, detail="Open trade not found")
    return {"ok": True, "close_command_id": out["command_id"],
            "close_seq": out["commands"][0]["close_seq"]}



# Statuses that are SAFE to bulk-delete. Open & pending trades are intentionally
# excluded — they represent real money on the broker side and must be closed
# properly via /trades/{id}/close, not silently wiped.
_DELETABLE_STATUSES = {"closed", "cancelled", "failed"}


@router.delete("")
async def bulk_clear_trades(
    scope: str = "all",
    older_than_days: Optional[int] = None,
    user=Depends(get_current_user),
):
    """Bulk-delete CLOSED / CANCELLED / FAILED trades for the current user.

    Open and pending trades are NEVER deletable through this endpoint —
    use /trades/{id}/close first.

    Query params:
      scope=all        → all closed + cancelled + failed (default)
      scope=closed     → only fully-closed trades
      scope=cancelled  → only cancelled
      scope=failed     → only failed
      older_than_days  → restrict to trades whose closed_at OR opened_at is
                         older than N days
    """
    db = get_db()
    if scope == "all":
        status_filter = {"$in": list(_DELETABLE_STATUSES)}
    elif scope in _DELETABLE_STATUSES:
        status_filter = scope
    else:
        raise HTTPException(
            status_code=400,
            detail=f"scope must be one of: all, {', '.join(sorted(_DELETABLE_STATUSES))}",
        )

    q: dict = {"user_id": user["id"], "status": status_filter}

    if older_than_days is not None:
        if older_than_days < 0:
            raise HTTPException(status_code=400, detail="older_than_days must be ≥ 0")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        # Match trades whose closed_at < cutoff (preferred) OR opened_at < cutoff
        # when closed_at is missing (e.g. cancelled-before-open trades).
        q["$or"] = [
            {"closed_at": {"$lt": cutoff}},
            {"closed_at": None, "opened_at": {"$lt": cutoff}},
        ]

    result = await db.trades.delete_many(q)
    return {
        "ok": True,
        "deleted": result.deleted_count,
        "scope": scope,
        "older_than_days": older_than_days,
    }
