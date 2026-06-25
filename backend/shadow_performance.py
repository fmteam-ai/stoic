"""Shadow Performance — compute hypothetical outcomes for paper-shadow signals.

When a bot runs in `paper_shadow_mode`, every emitted signal carries
`origin="shadow"` and IS NEVER EXECUTED. To turn those signals into a
meaningful track record, we replay subsequent daily candles and decide:

  - For a BUY signal: did the high ≥ TP before the low ≤ SL? → "tp" win.
                      Conversely → "sl" loss. Neither → "expired" (still open).
  - For a SELL signal: symmetric.
  - For a HOLD signal: skipped (no entry/SL/TP).

Outcomes are cached on the signal doc itself (`shadow_outcome` field) so
subsequent requests are fast. Resolution is end-of-day (daily candles); for
intraday accuracy we'd need an M5/M15 history endpoint — out of scope for v1.

The aggregator (`compute_aggregate`) returns:
  - Total shadow signals (and how many resolved)
  - Win-rate, average win / loss
  - Hypothetical $ P&L (per-lot, normalised — we don't know what lot size
    the user WOULD have used)
  - Sharpe-lite (mean / stdev of per-signal returns)
  - Per-symbol breakdown
"""
from __future__ import annotations
import logging
import math
from datetime import datetime, timezone
from typing import Optional

from market import get_history

logger = logging.getLogger("shadow.perf")


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


async def _replay_outcome(signal: dict) -> Optional[dict]:
    """Replay daily candles since the signal was emitted; return outcome dict
    or None if not enough data yet.
    """
    action = (signal.get("action") or "").upper()
    if action not in ("BUY", "SELL"):
        return None
    entry = signal.get("entry_price")
    sl = signal.get("stop_loss")
    tp = signal.get("take_profit")
    if entry is None or sl is None or tp is None:
        return None
    symbol = signal.get("symbol")
    if not symbol:
        return None
    sig_at = _parse_iso(signal.get("created_at") or signal.get("createdAt"))
    if not sig_at:
        return None

    try:
        history = await get_history(symbol)
    except Exception as e:
        logger.warning("shadow replay: get_history(%s) failed: %s", symbol, e)
        return None

    # Filter candles to those AFTER the signal was emitted.
    # `get_history` returns daily candles with `date` field as ISO date string.
    future = []
    for c in history or []:
        d = c.get("date")
        if not d:
            continue
        try:
            cdt = datetime.fromisoformat(str(d)).replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if cdt.date() > sig_at.date():
            future.append((cdt, c))

    if not future:
        return None  # not enough data yet — outcome unresolved

    for cdt, c in future:
        hi = float(c.get("high") or 0)
        lo = float(c.get("low") or 0)
        if action == "BUY":
            # Conservative: if BOTH TP and SL hit in same candle, assume SL hit
            # first (bad-case scenario for the user).
            if lo <= float(sl):
                return _outcome("sl", entry, sl, action, cdt, sig_at, signal)
            if hi >= float(tp):
                return _outcome("tp", entry, tp, action, cdt, sig_at, signal)
        else:  # SELL
            if hi >= float(sl):
                return _outcome("sl", entry, sl, action, cdt, sig_at, signal)
            if lo <= float(tp):
                return _outcome("tp", entry, tp, action, cdt, sig_at, signal)

    # Still open after all available candles — give current MTM
    last = future[-1][1]
    last_close = float(last.get("close") or entry)
    return _outcome("open", entry, last_close, action, future[-1][0], sig_at, signal)


def _outcome(hit: str, entry: float, exit_px: float, action: str,
             cdt: datetime, sig_at: datetime, signal: dict) -> dict:
    direction = 1 if action == "BUY" else -1
    pnl_pts = direction * (exit_px - entry)
    sl_pts = abs(entry - float(signal.get("stop_loss") or entry))
    r_multiple = (pnl_pts / sl_pts) if sl_pts > 0 else 0
    return {
        "hit": hit,
        "exit_price": round(exit_px, 5),
        "pnl_points": round(pnl_pts, 5),
        "r_multiple": round(r_multiple, 3),
        "days_to_outcome": (cdt.date() - sig_at.date()).days,
        "resolved_at": cdt.isoformat(),
    }


async def compute_or_get_outcome(db, signal: dict) -> Optional[dict]:
    """Cache-on-doc helper. Re-uses `shadow_outcome` if present + closed."""
    cached = signal.get("shadow_outcome")
    if cached and cached.get("hit") in ("tp", "sl"):
        return cached  # locked-in outcome, never recompute

    outcome = await _replay_outcome(signal)
    if outcome:
        try:
            await db.signals.update_one(
                {"_id": signal["_id"]},
                {"$set": {"shadow_outcome": outcome}},
            )
        except Exception:  # noqa: BLE001
            pass
    return outcome


async def compute_aggregate(db, user_id: str, *, since_days: int = 90,
                            limit: int = 500) -> dict:
    """Aggregate shadow performance for one user."""
    cursor = db.signals.find({
        "user_id": user_id,
        "origin": "shadow",
    }).sort("created_at", -1).limit(limit)
    sigs = await cursor.to_list(length=limit)

    rows = []
    wins = losses = open_n = unresolved = 0
    total_r = 0.0
    sum_win_r = sum_loss_r = 0.0
    r_samples: list[float] = []
    by_symbol: dict[str, dict] = {}

    for s in sigs:
        outcome = await compute_or_get_outcome(db, s)
        sym = s.get("symbol") or "?"
        bs = by_symbol.setdefault(sym, {"signals": 0, "wins": 0, "losses": 0, "open": 0, "total_r": 0.0})
        bs["signals"] += 1

        row = {
            "id": str(s["_id"]),
            "symbol": sym,
            "action": s.get("action"),
            "confidence": s.get("confidence"),
            "entry_price": s.get("entry_price"),
            "stop_loss": s.get("stop_loss"),
            "take_profit": s.get("take_profit"),
            "created_at": s.get("created_at") if isinstance(s.get("created_at"), str)
                          else (s.get("created_at").isoformat() if s.get("created_at") else None),
            "outcome": outcome,
        }
        rows.append(row)

        if outcome is None:
            unresolved += 1
            continue
        r = outcome.get("r_multiple") or 0
        r_samples.append(r)
        total_r += r
        bs["total_r"] += r
        if outcome["hit"] == "tp":
            wins += 1; bs["wins"] += 1; sum_win_r += r
        elif outcome["hit"] == "sl":
            losses += 1; bs["losses"] += 1; sum_loss_r += r
        else:  # open
            open_n += 1; bs["open"] += 1

    resolved = wins + losses
    win_rate = (wins / resolved) if resolved else None
    avg_win_r = (sum_win_r / wins) if wins else None
    avg_loss_r = (sum_loss_r / losses) if losses else None
    expectancy_r = ((avg_win_r or 0) * (win_rate or 0) +
                    (avg_loss_r or 0) * (1 - (win_rate or 0))) if win_rate is not None else None

    # Sharpe-lite (per-signal R)
    sharpe = None
    if len(r_samples) > 1:
        mean = sum(r_samples) / len(r_samples)
        var = sum((x - mean) ** 2 for x in r_samples) / (len(r_samples) - 1)
        sd = math.sqrt(var) if var > 0 else 0
        sharpe = round(mean / sd, 3) if sd > 0 else None

    return {
        "total_signals": len(sigs),
        "resolved": resolved,
        "open": open_n,
        "unresolved": unresolved,
        "wins": wins,
        "losses": losses,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "total_r": round(total_r, 3),
        "avg_win_r": round(avg_win_r, 3) if avg_win_r is not None else None,
        "avg_loss_r": round(avg_loss_r, 3) if avg_loss_r is not None else None,
        "expectancy_r": round(expectancy_r, 3) if expectancy_r is not None else None,
        "sharpe_lite": sharpe,
        "by_symbol": [
            {"symbol": k, **v,
             "win_rate": (round(v["wins"] / (v["wins"] + v["losses"]), 4)
                          if (v["wins"] + v["losses"]) else None)}
            for k, v in sorted(by_symbol.items(), key=lambda x: -x[1]["signals"])
        ],
        "rows": rows,
    }
