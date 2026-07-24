"""Professional operator tools (Phase 7).

  REPLAY MODE        tick-by-tick playback of a trade's lifetime window from
                     the recorded price_ticks / scalp_ticks streams (M15 bar
                     fallback for windows without tick coverage).
  DECISION TIMELINE  canonical 8-stage lifecycle, each stage resolved from
                     evidence recorded at the time:
                     signal → risk → order_check → broker → deal →
                     protection → reconciliation → journal
  WHAT-IF ANALYSIS   re-simulates the user's actual closed trades on the
                     recorded bars with altered parameters (risk %, SL/TP
                     geometry, trailing start) and compares to reality.
"""
import logging
from datetime import datetime, timedelta, timezone

from pip_utils import base_symbol

logger = logging.getLogger("operator-tools")

MAX_REPLAY_POINTS = 2000


def _ts(v):
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------ replay mode
async def trade_replay(db, trade: dict) -> dict:
    sym = base_symbol(trade.get("symbol") or "")
    t0 = _ts(trade.get("opened_at") or trade.get("_dispatched_at"))
    t1 = _ts(trade.get("closed_at")) or (
        datetime.now(timezone.utc).timestamp())
    if not t0:
        return {"source": "none", "ticks": [],
                "note": "trade has no open timestamp"}
    lo, hi = t0 - 300, t1 + 300
    ticks = []
    async for p in db.price_ticks.find(
            {"symbol": {"$regex": f"^{sym}"},
             "ts": {"$gte": lo, "$lte": hi}}).sort("ts", 1).limit(20000):
        ticks.append({"t": float(p["ts"]),
                      "price": float(p.get("price") or p.get("bid") or 0),
                      "bid": p.get("bid"), "ask": p.get("ask")})
    async for batch in db.scalp_ticks.find(
            {"user_id": trade["user_id"], "symbol": {"$regex": f"^{sym}"},
             "last_ms": {"$gte": lo * 1000}, "first_ms": {"$lte": hi * 1000}}
            ).limit(200):
        for tk in batch.get("ticks") or []:
            tms = float(tk.get("t") or tk.get("ms") or 0)
            ts = tms / 1000.0 if tms > 1e11 else tms
            if lo <= ts <= hi:
                px = tk.get("bid") or tk.get("price") or tk.get("last")
                if px:
                    ticks.append({"t": ts, "price": float(px),
                                  "bid": tk.get("bid"), "ask": tk.get("ask")})
    source = "ticks"
    if len(ticks) < 10:
        source = "m15_bars"
        cdoc = await db.intraday_candles.find_one(
            {"user_id": trade["user_id"], "symbol": sym})
        ticks = [{"t": float(b["t"]), "price": float(b["c"]),
                  "high": b["h"], "low": b["l"]}
                 for b in (cdoc or {}).get("bars") or []
                 if lo - 900 <= float(b["t"]) <= hi + 900]
    ticks.sort(key=lambda x: x["t"])
    seen, dedup = set(), []
    for tk in ticks:
        if tk["t"] not in seen:
            seen.add(tk["t"])
            dedup.append(tk)
    ticks = dedup
    if len(ticks) > MAX_REPLAY_POINTS:
        step = len(ticks) / MAX_REPLAY_POINTS
        ticks = [ticks[int(i * step)] for i in range(MAX_REPLAY_POINTS)]

    markers = []
    if t0:
        markers.append({"t": t0, "kind": "ENTRY",
                        "price": trade.get("entry_price"),
                        "label": f"{trade.get('action')} @ {trade.get('entry_price')}"})
    async for e in db.trade_events.find(
            {"trade_id": str(trade["_id"])}).sort("ts_ms", 1).limit(100):
        et = _ts(e.get("occurred_at"))
        if et:
            markers.append({"t": et, "kind": e.get("event_type"),
                            "label": (e.get("payload") or {}).get("detail", "")[:80]})
    if trade.get("closed_at"):
        markers.append({"t": _ts(trade["closed_at"]), "kind": "EXIT",
                        "price": trade.get("exit_price"),
                        "label": f"exit @ {trade.get('exit_price')} "
                                 f"(${trade.get('pnl')})"})
    return {"source": source, "symbol": sym,
            "ticks": ticks, "markers": markers,
            "levels": {"entry": trade.get("entry_price"),
                       "stop_loss": trade.get("original_stop_loss")
                       or trade.get("stop_loss"),
                       "tp1": trade.get("tp1"), "tp2": trade.get("tp2"),
                       "tp3": trade.get("tp3")},
            "note": None if source == "ticks" else
            "no tick coverage for this window — showing M15 closes"}


# ------------------------------------------------------ decision timeline
STAGES = ("signal", "risk", "order_check", "broker", "deal",
          "protection", "reconciliation", "journal")


async def decision_timeline(db, trade: dict) -> dict:
    from bson import ObjectId
    signal = {}
    if trade.get("signal_id"):
        try:
            signal = await db.signals.find_one(
                {"_id": ObjectId(str(trade["signal_id"]))}) or {}
        except Exception:
            pass
    deals = []
    pid = trade.get("position_id") or trade.get("mt5_ticket")
    if pid:
        async for d in db.broker_deals.find(
                {"account_id": trade.get("account_id"), "mt5_ticket": pid}
                ).sort("deal_time", 1).limit(20):
            deals.append(d)
    journal = await db.journal_cards.find_one({"trade_id": str(trade["_id"])})
    prot_at = trade.get("exit_last_tighten_at")
    prot_done = bool(trade.get("breakeven_set") or trade.get("trail_active")
                     or trade.get("tp1_closed") or prot_at
                     or trade.get("confirmed_stop_loss"))
    stages = {
        "signal": {
            "done": bool(signal),
            "at": str(signal.get("created_at") or "") or None,
            "detail": (f"{signal.get('engine_label') or 'engine'} signal, "
                       f"confidence {signal.get('confidence')}%") if signal
            else "signal document not retained"},
        "risk": {
            "done": bool(signal.get("risk_engine") or signal.get("pipeline_safe_to_execute")
                         or trade.get("sl_pips")),
            "at": None,
            "detail": f"sized at {trade.get('lot_size')} lots, "
                      f"SL {trade.get('sl_pips')} pips"
                      + (" — risk engine approved" if signal.get("risk_engine") else "")},
        "order_check": {
            "done": bool(trade.get("_dispatched_at")) and not trade.get("preflight_rejected"),
            "at": trade.get("_dispatched_at"),
            "detail": ("broker preflight REJECTED: " + str(trade.get("preflight_error"))
                       if trade.get("preflight_rejected") else
                       f"dispatched to EA (attempt {trade.get('_dispatch_count') or 1}) "
                       f"— OrderCheck preflight passed")},
        "broker": {
            "done": bool(trade.get("mt5_ticket")),
            "at": trade.get("acknowledged_at"),
            "detail": (f"accepted — ticket #{trade.get('mt5_ticket')}, filled "
                       f"{trade.get('entry_price')}"
                       + (f" ({trade.get('slippage_pips'):+.1f}p slippage)"
                          if trade.get("slippage_pips") is not None else "")
                       if trade.get("mt5_ticket") else
                       f"not accepted ({trade.get('status')})")},
        "deal": {
            "done": bool(deals),
            "at": (datetime.fromtimestamp(float(deals[0]["deal_time"]),
                                          tz=timezone.utc).isoformat()
                   if deals and deals[0].get("deal_time") else None),
            "detail": f"{len(deals)} broker deal(s) recorded" if deals
            else "no broker deals pushed yet"},
        "protection": {
            "done": prot_done,
            "at": prot_at,
            "detail": ", ".join(filter(None, [
                "break-even set" if trade.get("breakeven_set") else None,
                "trailing active" if trade.get("trail_active") else None,
                "TP1 banked" if trade.get("tp1_closed") else None,
                "stop broker-confirmed" if trade.get("confirmed_stop_loss") else None,
            ])) or "server-side tiers armed — no protection action needed yet"},
        "reconciliation": {
            "done": bool(trade.get("reconciled") or
                         (trade.get("status") == "closed"
                          and trade.get("pnl") is not None
                          and not trade.get("pnl_unknown"))),
            "at": trade.get("reconciled_at") or trade.get("closed_at"),
            "detail": (f"P&L ${trade.get('pnl')} reconciled against broker deals"
                       if trade.get("status") == "closed" else
                       "reconciles on close against broker deal history")},
        "journal": {
            "done": bool(journal or trade.get("self_evaluated")),
            "at": None,
            "detail": ((journal or {}).get("headline")
                       or (journal or {}).get("summary")
                       or ("self-evaluation recorded" if trade.get("self_evaluated")
                           else "journal card not generated yet"))[:160]},
    }
    out = []
    for s in STAGES:
        row = stages[s]
        out.append({"stage": s, "status": "complete" if row["done"] else "pending",
                    "at": row["at"], "detail": row["detail"]})
    return {"trade_id": str(trade["_id"]), "stages": out,
            "complete": sum(1 for r in out if r["status"] == "complete")}


# ---------------------------------------------------------- what-if engine
def simulate_exit(action: str, entry: float, sl: float, tp: float,
                  bars: list, trailing_start_r: float | None = None,
                  trail_dist_r: float = 0.5) -> dict:
    """Conservative bar-walk (SL checked before TP inside a bar). Returns
    exit price, reason and R multiple."""
    risk = abs(entry - sl)
    if risk <= 0 or not bars:
        return {"exit": entry, "reason": "no_data", "r": 0.0}
    cur_sl, best = sl, entry
    for b in bars:
        h, low = float(b["h"]), float(b["l"])
        if action == "BUY":
            if low <= cur_sl:
                return {"exit": cur_sl, "reason": "sl",
                        "r": (cur_sl - entry) / risk}
            if tp and h >= tp:
                return {"exit": tp, "reason": "tp", "r": (tp - entry) / risk}
            best = max(best, h)
            if trailing_start_r and best - entry >= trailing_start_r * risk:
                cur_sl = max(cur_sl, best - trail_dist_r * risk)
        else:
            if h >= cur_sl:
                return {"exit": cur_sl, "reason": "sl",
                        "r": (entry - cur_sl) / risk}
            if tp and low <= tp:
                return {"exit": tp, "reason": "tp", "r": (entry - tp) / risk}
            best = min(best, low)
            if trailing_start_r and entry - best >= trailing_start_r * risk:
                cur_sl = min(cur_sl, best + trail_dist_r * risk)
    last = float(bars[-1]["c"])
    r = (last - entry) / risk if action == "BUY" else (entry - last) / risk
    return {"exit": last, "reason": "window_end", "r": r}


def _equity_stats(pnls: list) -> dict:
    total = sum(pnls)
    peak, dd, cum = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    wins = sum(1 for p in pnls if p > 0)
    return {"total_pnl": round(total, 2), "max_drawdown": round(dd, 2),
            "win_rate": round(wins / len(pnls), 2) if pnls else 0.0,
            "n": len(pnls)}


async def what_if(db, user_id: str, *, days: int = 30,
                  risk_pct: float | None = None,
                  sl_mult: float | None = None,
                  tp_mult: float | None = None,
                  trailing_start_r: float | None = None) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    trades = await db.trades.find(
        {"user_id": user_id, "origin": "auto", "status": "closed",
         "pnl": {"$ne": None}, "closed_at": {"$gte": since},
         "entry_price": {"$ne": None}},
        {"pnl": 1, "symbol": 1, "action": 1, "entry_price": 1,
         "stop_loss": 1, "original_stop_loss": 1, "take_profit": 1,
         "tp1": 1, "opened_at": 1, "risk_pct": 1, "lot_size": 1}
    ).sort("closed_at", 1).to_list(2000)
    if not trades:
        return {"error": "no closed bot trades in the window", "n": 0}

    geometry = any(v is not None for v in (sl_mult, tp_mult, trailing_start_r))
    baseline_pnls, sim_pnls, covered = [], [], 0
    bar_cache = {}
    for t in trades:
        actual = float(t["pnl"])
        scale = 1.0
        if risk_pct is not None:
            orig = float(t.get("risk_pct") or 1.0)
            scale = risk_pct / orig if orig > 0 else 1.0
        if not geometry:
            baseline_pnls.append(actual)
            sim_pnls.append(actual * scale)
            covered += 1
            continue
        sym = base_symbol(t.get("symbol") or "")
        if sym not in bar_cache:
            cdoc = await db.intraday_candles.find_one(
                {"user_id": user_id, "symbol": sym}, {"bars": 1})
            bar_cache[sym] = (cdoc or {}).get("bars") or []
        t0 = _ts(t.get("opened_at"))
        bars = [b for b in bar_cache[sym] if t0 and float(b["t"]) >= t0][:288]
        entry = float(t["entry_price"])
        sl0 = float(t.get("original_stop_loss") or t.get("stop_loss") or 0)
        tp0 = float(t.get("tp1") or t.get("take_profit") or 0)
        if not bars or not sl0:
            continue  # no recorded path — honest skip
        risk = abs(entry - sl0)
        risk_usd = abs(actual) if actual else risk * float(t.get("lot_size") or 0.01) * 100
        base = simulate_exit(t["action"], entry, sl0, tp0, bars)
        new_sl = entry - risk * (sl_mult or 1.0) if t["action"] == "BUY" \
            else entry + risk * (sl_mult or 1.0)
        new_tp = (entry + abs(entry - tp0) * (tp_mult or 1.0)
                  if t["action"] == "BUY"
                  else entry - abs(entry - tp0) * (tp_mult or 1.0)) if tp0 else 0
        sim = simulate_exit(t["action"], entry, new_sl, new_tp, bars,
                            trailing_start_r=trailing_start_r)
        r_usd = risk_usd / max(abs(base["r"]), 0.25)
        baseline_pnls.append(round(base["r"] * r_usd, 2))
        sim_pnls.append(round(sim["r"] * r_usd * scale, 2))
        covered += 1

    baseline = _equity_stats(baseline_pnls)
    simulated = _equity_stats(sim_pnls)
    return {"scenario": {k: v for k, v in
                         (("risk_pct", risk_pct), ("sl_mult", sl_mult),
                          ("tp_mult", tp_mult),
                          ("trailing_start_r", trailing_start_r))
                         if v is not None},
            "window_days": days, "mode": "bar_replay" if geometry else "risk_rescale",
            "coverage": {"simulated": covered, "total": len(trades)},
            "baseline": baseline, "simulated": simulated,
            "delta": {"total_pnl": round(simulated["total_pnl"]
                                         - baseline["total_pnl"], 2),
                      "max_drawdown": round(simulated["max_drawdown"]
                                            - baseline["max_drawdown"], 2)},
            "note": ("geometry scenarios replay recorded M15 bars — trades "
                     "without retained bars are skipped" if geometry else
                     "risk-% scenarios rescale each trade's actual P&L")}
