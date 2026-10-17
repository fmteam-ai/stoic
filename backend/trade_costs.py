"""v1.60.63 #85 — trade cost ledger + smart spread gate (SHADOW first). Per symbol by design; gold priority.

Costs ≈ 0.04–0.10 R per trade — the size of the scalpers' gross edge — so they are measured per broker and gated:
A) ledger: exit slippage (signed, + = worse) vs the level that triggered the close, exit spread from the latest
   heartbeat, cost_summary (pips, $, R vs ORIGINAL stop, % of ORIGINAL TP) on every closed trade; heartbeat spread
   samples per account+symbol+UTC hour (rolling 14 days, TTL); per-broker report.
B) gate (before dispatch, after every existing filter, all engines): block when spread > mult × that account's median
   spread for this UTC hour (n ≥ 30, else the static cap), or expected round-trip cost > max % of the planned TP.
   mode "shadow" (default) never blocks — it stamps signal.spread_gate and counts; "enforce" skips with a pulse."""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from pip_utils import base_symbol, pip_size, pip_value_usd_per_lot, price_to_pips

HOUR_MIN_SAMPLES = 30
HISTORY_DAYS = 14
SAMPLES_PER_HOUR_PER_DAY = 60
GATE_MODES = ("off", "shadow", "enforce")
DEFAULT_MODE, DEFAULT_MULT, DEFAULT_MAX_COST_PCT = "shadow", 1.5, 12.0
SESSIONS = (("asia", 0, 7), ("london", 7, 13), ("ny", 13, 21), ("late", 21, 24))


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def session_of(hour_utc: int) -> str:
    for name, a, b in SESSIONS:
        if a <= int(hour_utc) % 24 < b:
            return name
    return "late"


# ── A1. exit slippage ────────────────────────────────────────────────────────────────────────────────────────
def exit_slippage_pips(trade: dict, exit_price: float | None = None) -> tuple[float | None, str | None]:
    """(signed pips, kind) — the broker close price vs the level that triggered it (SL or TP live at close, incl. a
    BE-moved stop; `scalp.reports.broker_exit_kind`). Positive = worse for us. (None, None) when the close was
    neither (manual / time-stop / unknown) or no exit price exists."""
    from scalp.reports import broker_exit_kind
    px = _f(exit_price if exit_price is not None else trade.get("exit_price"))
    if px is None:
        return None, None
    kind = broker_exit_kind({**trade, "exit_price": px})
    if kind is None:
        return None, None
    level = _f(trade.get("stop_loss")) if kind == "stop_loss" else _f(trade.get("take_profit"))
    if level is None:
        level = _f(trade.get("original_stop_loss") if kind == "stop_loss" else trade.get("original_take_profit"))
    if level is None:
        return None, kind
    worse = (level - px) if trade.get("action") == "BUY" else (px - level)
    return round(price_to_pips(trade.get("symbol"), worse), 2), kind


# ── A2. cost summary ─────────────────────────────────────────────────────────────────────────────────────────
def cost_summary(trade: dict, exit_spread_pips: float | None = None, exit_price: float | None = None) -> dict:
    """pips and $ for spread (entry), entry slippage, exit slippage, commission (round trip); totals in pips, $,
    R of the ORIGINAL stop distance and % of the ORIGINAL TP distance. Missing inputs count 0 and are listed."""
    sym = trade.get("symbol")
    lots = _f(trade.get("lot_size")) or 0.0
    pip_usd = _f(trade.get("pip_value_usd_per_lot")) or pip_value_usd_per_lot(sym, trade.get("account_type"), price=_f(trade.get("entry_price"))) or 0.0
    slip_exit, kind = exit_slippage_pips(trade, exit_price)
    parts = {"spread_entry": _f(trade.get("spread_at_entry_pips")), "slip_entry": _f(trade.get("slippage_pips")),
             "slip_exit": slip_exit, "commission": _f(trade.get("commission_pips"))}
    missing = [k for k, v in parts.items() if v is None]
    pips = {k: round(v or 0.0, 3) for k, v in parts.items()}
    usd = {k: round(v * pip_usd * lots, 2) for k, v in pips.items()}
    total_pips = round(sum(pips.values()), 3)
    entry = _f(trade.get("entry_price"))
    osl = _f(trade.get("original_stop_loss")) or _f(trade.get("stop_loss"))
    otp = _f(trade.get("original_take_profit")) or _f(trade.get("take_profit"))
    sl_pips = price_to_pips(sym, abs(entry - osl)) if entry is not None and osl is not None else None
    tp_pips = price_to_pips(sym, abs(otp - entry)) if entry is not None and otp is not None else None
    return {"pips": pips, "usd": usd, "total_cost_pips": total_pips, "total_cost_usd": round(total_pips * pip_usd * lots, 2),
            "total_cost_R": round(total_pips / sl_pips, 4) if sl_pips else None,
            "cost_pct_of_tp": round(100.0 * total_pips / tp_pips, 2) if tp_pips else None,
            "exit_kind": kind, "exit_spread_pips": exit_spread_pips, "pip_size": pip_size(sym), "pip_usd_per_lot": pip_usd,
            "missing": missing, "computed_at": datetime.now(timezone.utc).isoformat()}


# ── A3. hour-of-day spread history ───────────────────────────────────────────────────────────────────────────
def _day(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


async def ensure_indexes(db) -> None:
    await db.spread_history.create_index("expires_at", expireAfterSeconds=0)
    await db.spread_history.create_index([("account_id", 1), ("symbol", 1), ("day", 1)], unique=True)


async def record_spread_samples(db, account_id: str, spreads: dict | None, now: datetime | None = None) -> int:
    """Heartbeat hook — one sample per symbol into the (account, symbol, day) doc under its UTC hour; ≤ 60 per hour."""
    if not spreads:
        return 0
    now = now or datetime.now(timezone.utc)
    hour = str(now.astimezone(timezone.utc).hour)
    n = 0
    for sym, sp in spreads.items():
        v = _f(sp)
        if v is None or v < 0:
            continue
        await db.spread_history.update_one(
            {"account_id": str(account_id), "symbol": base_symbol(sym), "day": _day(now)},
            {"$push": {f"hours.{hour}": {"$each": [round(v, 2)], "$slice": -SAMPLES_PER_HOUR_PER_DAY}},
             "$setOnInsert": {"expires_at": now + timedelta(days=HISTORY_DAYS + 1)}},
            upsert=True)
        n += 1
    return n


def hour_stats_from_docs(docs: list, hour: int) -> dict:
    vals = []
    for d in docs:
        vals.extend(float(x) for x in ((d.get("hours") or {}).get(str(int(hour))) or []))
    if not vals:
        return {"n": 0, "median": None, "p90": None}
    s = sorted(vals)
    return {"n": len(s), "median": round(statistics.median(s), 2), "p90": round(s[min(len(s) - 1, int(0.9 * len(s)))], 2)}


async def hour_stats(db, account_id: str, symbol: str, hour: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = _day(now - timedelta(days=HISTORY_DAYS))
    docs = await db.spread_history.find({"account_id": str(account_id), "symbol": base_symbol(symbol), "day": {"$gte": since}},
                                        {f"hours.{int(hour)}": 1}).to_list(HISTORY_DAYS + 1)
    return hour_stats_from_docs(docs, hour)


async def session_spreads(db, account_id: str, symbol: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = _day(now - timedelta(days=HISTORY_DAYS))
    docs = await db.spread_history.find({"account_id": str(account_id), "symbol": base_symbol(symbol), "day": {"$gte": since}}).to_list(HISTORY_DAYS + 1)
    out = {}
    for name, a, b in SESSIONS:
        vals = []
        for d in docs:
            for h in range(a, b):
                vals.extend(float(x) for x in ((d.get("hours") or {}).get(str(h)) or []))
        out[name] = {"n": len(vals), "avg": round(sum(vals) / len(vals), 2) if vals else None,
                     "median": round(statistics.median(vals), 2) if vals else None}
    return out


async def median_entry_slippage(db, account_id: str, symbol: str, days: int = HISTORY_DAYS) -> float | None:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = await db.trades.find({"account_id": str(account_id), "symbol": {"$regex": f"^{base_symbol(symbol)}", "$options": "i"},
                                 "slippage_pips": {"$ne": None}, "created_at": {"$gte": since}},
                                {"slippage_pips": 1}).sort("created_at", -1).to_list(200)
    vals = [float(r["slippage_pips"]) for r in rows if _f(r.get("slippage_pips")) is not None]
    return round(statistics.median(vals), 2) if vals else None


# ── B. smart spread gate ─────────────────────────────────────────────────────────────────────────────────────
def gate_mode(cfg: dict | None) -> str:
    m = str((cfg or {}).get("spread_gate_mode") or DEFAULT_MODE).lower()
    return m if m in GATE_MODES else DEFAULT_MODE


def spread_gate_decision(cfg: dict | None, *, symbol: str, spread_now: float | None, hour: dict | None, static_cap: float | None,
                         tp_pips: float | None, median_slip_entry: float | None, commission_pips: float | None) -> dict:
    """Pure. → {would_block, reason, rule, spread, median, n, mult, cost_pct, max_cost_pct, basis}"""
    mult = _f((cfg or {}).get("spread_gate_mult")) or DEFAULT_MULT
    max_pct = _f((cfg or {}).get("max_cost_pct_of_tp")) or DEFAULT_MAX_COST_PCT
    sp = _f(spread_now)
    med, n = (_f((hour or {}).get("median")), int((hour or {}).get("n") or 0))
    out = {"would_block": False, "reason": None, "rule": None, "spread": sp, "median": med, "n": n, "mult": mult,
           "cost_pct": None, "max_cost_pct": max_pct, "basis": None, "symbol": base_symbol(symbol)}
    if sp is None:
        out["basis"] = "no spread"
        return out
    if n >= HOUR_MIN_SAMPLES and med is not None and med > 0:
        out["basis"] = "hour_median"
        if sp > mult * med:
            out.update(would_block=True, rule="spread_vs_hour_median",
                       reason=f"Spread gate — {out['symbol']} spread {sp:.1f}p > {mult:g}× hour median {med:.1f}p")
    elif _f(static_cap):
        out["basis"] = "static_cap"
        if sp > float(static_cap):
            out.update(would_block=True, rule="spread_vs_static_cap",
                       reason=f"Spread gate — {out['symbol']} spread {sp:.1f}p > static cap {float(static_cap):.1f}p (hour median needs {HOUR_MIN_SAMPLES} samples, have {n})")
    else:
        out["basis"] = "no_reference"
    if tp_pips and tp_pips > 0:
        expected = sp + (median_slip_entry or 0.0) + (commission_pips or 0.0)
        out["cost_pct"] = round(100.0 * expected / float(tp_pips), 2)
        out["expected_cost_pips"] = round(expected, 2)
        if not out["would_block"] and out["cost_pct"] > max_pct:
            out.update(would_block=True, rule="cost_pct_of_tp",
                       reason=f"Spread gate — {out['symbol']} expected round-trip cost {expected:.1f}p = {out['cost_pct']:.0f}% of TP {float(tp_pips):.1f}p (max {max_pct:g}%)")
    return out


async def evaluate_gate(db, cfg: dict, account: dict, signal: dict, static_cap: float | None, now: datetime | None = None) -> dict:
    """Route/runner hook. Never raises in callers (they wrap it)."""
    from entry_costs import spread_at_entry
    now = now or datetime.now(timezone.utc)
    sym = signal.get("symbol") or ""
    entry, tp = _f(signal.get("entry_price")), _f(signal.get("take_profit"))
    tp_pips = price_to_pips(sym, abs(tp - entry)) if entry is not None and tp is not None else None
    acc_id = str(account.get("_id"))
    hour = await hour_stats(db, acc_id, sym, now.astimezone(timezone.utc).hour, now)
    med_slip = await median_entry_slippage(db, acc_id, sym)
    comm = await _typical_commission_pips(db, acc_id, sym)
    d = spread_gate_decision(cfg, symbol=sym, spread_now=spread_at_entry(account, sym), hour=hour, static_cap=static_cap,
                             tp_pips=tp_pips, median_slip_entry=med_slip, commission_pips=comm)
    d.update(mode=gate_mode(cfg), hour_utc=now.astimezone(timezone.utc).hour, account_id=acc_id, median_slip_entry=med_slip,
             commission_pips=comm, tp_pips=round(tp_pips, 2) if tp_pips else None)
    return d


async def _typical_commission_pips(db, account_id: str, symbol: str) -> float | None:
    row = await db.trades.find_one({"account_id": str(account_id), "symbol": {"$regex": f"^{base_symbol(symbol)}", "$options": "i"},
                                    "commission_pips": {"$ne": None}}, {"commission_pips": 1}, sort=[("created_at", -1)])
    return _f((row or {}).get("commission_pips"))


# ── A1/A2 on close ───────────────────────────────────────────────────────────────────────────────────────────
async def stamp_exit_costs(db, trade: dict, account: dict | None, exit_price: float | None = None) -> dict | None:
    """Called after the authoritative broker close (external OUT deal). Idempotent; fail-soft at the caller."""
    from entry_costs import spread_at_entry
    px = _f(exit_price if exit_price is not None else trade.get("exit_price"))
    if px is None:
        return None
    exit_spread = spread_at_entry(account, trade.get("symbol"))
    cs = cost_summary(trade, exit_spread, px)
    upd = {"cost_summary": cs, "exit_slippage_pips": cs["pips"]["slip_exit"] if "slip_exit" not in cs["missing"] else None,
           "exit_kind": cs["exit_kind"], "exit_spread_pips": exit_spread}
    await db.trades.update_one({"_id": trade["_id"]}, {"$set": upd})
    return upd


# ── A4 + B7. report ──────────────────────────────────────────────────────────────────────────────────────────
def _avg(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


def _r(trade: dict) -> float | None:
    pnl, risk = _f(trade.get("pnl")), _f(trade.get("risk_amount"))
    if pnl is None or not risk:
        return None
    return pnl / risk


def summarise_costs(trades: list[dict]) -> dict:
    """Pure per-broker cost summary from closed trades carrying cost_summary (+ shadow scoreboard from spread_gate)."""
    with_cs = [t for t in trades if isinstance(t.get("cost_summary"), dict)]
    out = {"trades": len(trades), "with_costs": len(with_cs),
           "avg_entry_slippage_pips": _avg([_f(t.get("slippage_pips")) for t in trades]),
           "avg_exit_slippage_pips": _avg([_f(t.get("exit_slippage_pips")) for t in trades]),
           "avg_spread_entry_pips": _avg([_f(t.get("spread_at_entry_pips")) for t in trades]),
           "avg_total_cost_R": _avg([(t["cost_summary"] or {}).get("total_cost_R") for t in with_cs]),
           "avg_cost_pct_of_tp": _avg([(t["cost_summary"] or {}).get("cost_pct_of_tp") for t in with_cs]),
           "total_cost_usd": round(sum(_f((t["cost_summary"] or {}).get("total_cost_usd")) or 0.0 for t in with_cs), 2)}
    worst = sorted(with_cs, key=lambda t: -(_f((t["cost_summary"] or {}).get("total_cost_R")) or 0.0))[:5]
    out["worst_trades"] = [{"trade_id": str(t.get("_id")), "symbol": t.get("symbol"), "action": t.get("action"), "closed_at": t.get("closed_at"),
                            "total_cost_R": t["cost_summary"].get("total_cost_R"), "cost_pct_of_tp": t["cost_summary"].get("cost_pct_of_tp"),
                            "total_cost_usd": t["cost_summary"].get("total_cost_usd"), "pnl": _f(t.get("pnl")), "exit_kind": t["cost_summary"].get("exit_kind")}
                           for t in worst]
    by_session: dict = {}
    for t in trades:
        ts = str(t.get("created_at") or "")
        try:
            hour = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).hour
        except ValueError:
            continue
        s = "news" if t.get("news_window") or (t.get("gold_trend_pulse") or {}).get("news_mode") else session_of(hour)
        by_session.setdefault(s, []).append(t)
    out["by_session"] = {s: {"trades": len(rows), "avg_spread_entry_pips": _avg([_f(t.get("spread_at_entry_pips")) for t in rows]),
                             "avg_entry_slippage_pips": _avg([_f(t.get("slippage_pips")) for t in rows]),
                             "avg_total_cost_R": _avg([(t.get("cost_summary") or {}).get("total_cost_R") for t in rows])}
                         for s, rows in by_session.items()}
    gated = [t for t in trades if (t.get("spread_gate") or {}).get("would_block")]
    passed = [t for t in trades if t.get("spread_gate") and not t["spread_gate"].get("would_block")]

    def _score(rows):
        rs = [_r(t) for t in rows if _r(t) is not None]
        wins = [t for t in rows if (_f(t.get("pnl")) or 0) > 0]
        return {"trades": len(rows), "win_rate": round(100.0 * len(wins) / len(rows), 1) if rows else None,
                "net_R": round(sum(rs), 2) if rs else None, "net_usd": round(sum(_f(t.get("pnl")) or 0.0 for t in rows), 2)}
    out["shadow_scoreboard"] = {"would_block": _score(gated), "passed": _score(passed),
                                "rules": {r: sum(1 for t in gated if (t["spread_gate"] or {}).get("rule") == r)
                                          for r in ("spread_vs_hour_median", "spread_vs_static_cap", "cost_pct_of_tp")}}
    return out


async def report(db, user_id: str, days: int = 7, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=max(1, min(int(days or 7), 90)))).isoformat()
    accounts = await db.accounts.find({"user_id": user_id}, {"label": 1, "broker_server": 1, "mode": 1, "current_spreads": 1}).to_list(50)
    trades = await db.trades.find({"user_id": user_id, "status": "closed", "closed_at": {"$gte": since}, "exit_price": {"$ne": None}},
                                  {"account_id": 1, "symbol": 1, "action": 1, "pnl": 1, "risk_amount": 1, "created_at": 1, "closed_at": 1,
                                   "spread_at_entry_pips": 1, "slippage_pips": 1, "exit_slippage_pips": 1, "cost_summary": 1,
                                   "spread_gate": 1, "news_window": 1, "gold_trend_pulse.news_mode": 1}).to_list(5000)
    brokers = []
    for a in accounts:
        aid = str(a["_id"])
        rows = [t for t in trades if str(t.get("account_id")) == aid]
        syms = sorted({base_symbol(t.get("symbol")) for t in rows} | {base_symbol(s) for s in (a.get("current_spreads") or {})})
        spreads = {s: await session_spreads(db, aid, s, now) for s in syms if s}
        brokers.append({"account_id": aid, "label": a.get("label") or a.get("broker_server"), "mode": a.get("mode"),
                        "symbols": syms, "spread_by_session": spreads, **summarise_costs(rows)})
    return {"days": days, "generated_at": now.isoformat(), "brokers": brokers, "all": summarise_costs(trades),
            "gate": {"mode": None, "hour_min_samples": HOUR_MIN_SAMPLES, "history_days": HISTORY_DAYS}}
