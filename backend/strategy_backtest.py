"""Strategy Backtest Preview.

Replays a compiled NL strategy against the user's own historical closed
trades and aggregates win-rate, P&L, and trade-count metrics so users can
sanity-check a compiled strategy *before* applying it to their live bot.

Inputs:
  compiled       — dict from nl_commander.build_strategy (or any object with
                   `symbols` / `session_preference` keys)
  user_id        — required, scopes to the user's own trade history
  lookback_days  — defaults to 30

What it filters by (best-effort — we only filter on fields actually stored
on each trade row):
  • symbols              — exact match against compiled.symbols
  • session_preference   — UTC-hour window: london (07-16), ny (12-21),
                           tokyo (23-08), any (no filter)
  • closed within lookback window

What it does NOT simulate:
  • risk_level / strategy_style — too coarse to differentiate fairly without
    re-running indicators. We surface these in `notes` instead.

Returns:
  {
    "lookback_days": int,
    "filters": { ... echoed ... },
    "total_trades": int,        # closed trades in window before filtering
    "matched_trades": int,      # trades that match symbol+session filters
    "wins": int, "losses": int,
    "win_rate": float | None,   # null when matched_trades == 0
    "total_pnl_usd": float,
    "avg_pnl_usd": float | None,
    "best_trade": float | None,
    "worst_trade": float | None,
    "by_symbol": { "XAUUSD": {trades, wins, win_rate, pnl}, ... },
    "notes": [str, ...],
    "evaluated_at": iso,
  }
"""
from datetime import datetime, timezone, timedelta
import logging

from database import get_db

logger = logging.getLogger("strategy-backtest")


# Approximate UTC trading-session windows. Kept intentionally simple — the
# backtest is a sanity check, not a tick-accurate simulator.
SESSION_WINDOWS = {
    "london": (7, 16),    # 07:00–15:59 UTC
    "ny":     (12, 21),   # 12:00–20:59 UTC
    "tokyo":  (23, 8),    # 23:00–07:59 UTC (wraps midnight)
    "any":    None,
}


def _in_session(hour_utc: int, session: str) -> bool:
    win = SESSION_WINDOWS.get(session)
    if win is None:
        return True
    start, end = win
    if start <= end:
        return start <= hour_utc < end
    # Wrap-around (tokyo)
    return hour_utc >= start or hour_utc < end


def _parse_iso(ts):
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _trade_hour_utc(t: dict) -> int | None:
    ts = (_parse_iso(t.get("opened_at")) or _parse_iso(t.get("created_at"))
          or _parse_iso(t.get("entered_at")))
    if ts is None:
        return None
    return ts.astimezone(timezone.utc).hour


async def run_backtest(*, compiled: dict, user_id: str,
                       lookback_days: int = 30) -> dict:
    db = get_db()
    lookback_days = max(1, min(int(lookback_days or 30), 365))
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

    symbols_filter = [s.upper() for s in (compiled.get("symbols") or [])]
    session_pref = (compiled.get("session_preference") or "any").lower()
    if session_pref not in SESSION_WINDOWS:
        session_pref = "any"

    q = {"user_id": user_id, "status": "closed",
         "closed_at": {"$gte": since}}
    if symbols_filter:
        q["symbol"] = {"$in": symbols_filter}

    cursor = db.trades.find(q).sort("closed_at", -1).limit(2000)
    trades = await cursor.to_list(length=2000)

    notes: list[str] = []
    if not trades:
        notes.append(
            f"No closed trades found in last {lookback_days}d matching "
            f"{symbols_filter or 'any symbol'}. "
            "Backtest needs trade history — run the bot in paper mode first."
        )

    # Pre-compute total before session-filter for transparency
    total_in_window = len(trades)

    matched: list[dict] = []
    for t in trades:
        if session_pref != "any":
            h = _trade_hour_utc(t)
            if h is None or not _in_session(h, session_pref):
                continue
        matched.append(t)

    wins = sum(1 for t in matched if float(t.get("pnl") or 0) > 0)
    losses = sum(1 for t in matched if float(t.get("pnl") or 0) < 0)
    pnls = [float(t.get("pnl") or 0) for t in matched]
    total_pnl = sum(pnls)

    by_symbol: dict[str, dict] = {}
    for t in matched:
        sym = (t.get("symbol") or "").upper()
        if not sym:
            continue
        row = by_symbol.setdefault(sym, {"trades": 0, "wins": 0,
                                        "losses": 0, "pnl": 0.0})
        row["trades"] += 1
        p = float(t.get("pnl") or 0)
        row["pnl"] += p
        if p > 0:
            row["wins"] += 1
        elif p < 0:
            row["losses"] += 1
    for sym, row in by_symbol.items():
        n = row["trades"]
        row["win_rate"] = round(row["wins"] / n, 4) if n else None
        row["pnl"] = round(row["pnl"], 2)

    win_rate = round(wins / len(matched), 4) if matched else None
    avg_pnl = round(total_pnl / len(matched), 2) if matched else None

    if matched:
        # Surface caveats so users don't over-trust the preview
        if compiled.get("risk_level"):
            notes.append(
                f"Risk level '{compiled['risk_level']}' is not back-tested — "
                "historical trades reflect the risk settings active at the "
                "time they were taken."
            )
        if compiled.get("strategy_style"):
            notes.append(
                f"Style '{compiled['strategy_style']}' is informational only — "
                "we don't re-run indicators against past candles."
            )
        if len(matched) < 20:
            notes.append(
                f"Only {len(matched)} matching trades — sample size too small "
                "for statistical confidence. Treat the numbers directionally."
            )

    return {
        "lookback_days": lookback_days,
        "filters": {
            "symbols": symbols_filter or ["(any)"],
            "session_preference": session_pref,
        },
        "total_trades": total_in_window,
        "matched_trades": len(matched),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "total_pnl_usd": round(total_pnl, 2),
        "avg_pnl_usd": avg_pnl,
        "best_trade": round(max(pnls), 2) if pnls else None,
        "worst_trade": round(min(pnls), 2) if pnls else None,
        "by_symbol": by_symbol,
        "notes": notes,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }
