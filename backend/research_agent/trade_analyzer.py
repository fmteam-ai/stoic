"""Trade Analyzer — find weaknesses in a user's recent closed-trade history.

Pure-deterministic. Aggregates the last N days of closed trades by various
dimensions (symbol, session, hour-of-day, action, risk_level if logged)
and surfaces the worst performers as the inputs to the Hypothesis Generator.

A "weakness" is any bucket with ≥5 trades AND below-average win-rate AND
negative-or-flat total P&L. We pick the worst N per dimension.

Output:
  {
    "trade_count": int,
    "lookback_days": int,
    "overall": {win_rate, total_pnl_usd, avg_pnl_usd, wins, losses},
    "weaknesses": [
      {"dimension": "symbol",    "bucket": "BTCUSD", "n": 18,
       "win_rate": 0.33, "pnl_usd": -125.40, "delta_vs_overall_wr": -0.18,
       "summary": "BTCUSD trades win 33% (vs 51% overall); -$125.40 P&L"},
      ...
    ],
    "strengths": [...],     # mirror — top buckets
    "notes": [str, ...],
  }
"""
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import logging

logger = logging.getLogger("research.trade-analyzer")


# UTC-hour session windows (same as strategy_backtest)
SESSION_WINDOWS = {
    "london": (7, 16),    # 07-15 UTC
    "ny":     (12, 21),
    "tokyo":  (23, 8),    # wraps midnight
}


def _session_of(hour_utc: int) -> str:
    for name, (start, end) in SESSION_WINDOWS.items():
        if start <= end:
            if start <= hour_utc < end:
                return name
        else:
            if hour_utc >= start or hour_utc < end:
                return name
    return "off_hours"


def _parse_ts(ts):
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def _bucket_stats(trades: list[dict], overall_wr: float) -> list[dict]:
    """Group + summarise. Returns list of {bucket, n, win_rate, pnl_usd, delta}."""
    out = []
    for bucket, lst in trades.items():
        n = len(lst)
        if n < 5:  # too small to draw conclusions
            continue
        wins = sum(1 for t in lst if float(t.get("pnl") or 0) > 0)
        wr = wins / n
        pnl = sum(float(t.get("pnl") or 0) for t in lst)
        out.append({
            "bucket": bucket, "n": n,
            "win_rate": round(wr, 4),
            "pnl_usd": round(pnl, 2),
            "delta_vs_overall_wr": round(wr - overall_wr, 4),
        })
    return out


async def analyze(db, *, user_id: str, lookback_days: int = 30,
                  min_bucket_n: int = 5) -> dict:
    """Produce a weaknesses/strengths report for the user's recent history."""
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    cursor = db.trades.find({
        "user_id": user_id, "status": "closed",
        "closed_at": {"$gte": since},
    }).limit(2000)
    trades = await cursor.to_list(length=2000)

    notes: list[str] = []
    if len(trades) < min_bucket_n:
        notes.append(
            f"Only {len(trades)} closed trades in last {lookback_days}d — "
            "self-improvement needs more sample. Keep the bot running and "
            "I'll have something useful for you tomorrow."
        )
        return {"trade_count": len(trades), "lookback_days": lookback_days,
                "overall": {"win_rate": None, "total_pnl_usd": 0.0,
                            "avg_pnl_usd": None, "wins": 0, "losses": 0},
                "weaknesses": [], "strengths": [], "notes": notes}

    wins = sum(1 for t in trades if float(t.get("pnl") or 0) > 0)
    losses = sum(1 for t in trades if float(t.get("pnl") or 0) < 0)
    total_pnl = sum(float(t.get("pnl") or 0) for t in trades)
    overall_wr = wins / len(trades)

    by_symbol  = defaultdict(list)
    by_session = defaultdict(list)
    by_hour    = defaultdict(list)
    by_action  = defaultdict(list)

    for t in trades:
        sym = (t.get("symbol") or "").upper() or "UNKNOWN"
        by_symbol[sym].append(t)
        ts = _parse_ts(t.get("opened_at") or t.get("created_at"))
        if ts:
            h = ts.astimezone(timezone.utc).hour
            by_session[_session_of(h)].append(t)
            by_hour[f"{h:02d}:00 UTC"].append(t)
        action = (t.get("action") or "").upper()
        if action in ("BUY", "SELL"):
            by_action[action].append(t)

    # Combine + tag with dimension
    rows: list[dict] = []
    for dim, src in [("symbol", by_symbol), ("session", by_session),
                     ("hour", by_hour), ("action", by_action)]:
        for r in _bucket_stats(src, overall_wr):
            rows.append({"dimension": dim, **r})

    # Weaknesses = win_rate worse than overall AND non-positive P&L
    weak = [
        {**r, "summary": (f"{r['bucket']} ({r['dimension']}) wins "
                          f"{r['win_rate']*100:.0f}% vs overall "
                          f"{overall_wr*100:.0f}% · ${r['pnl_usd']:+.2f}")}
        for r in rows
        if r["win_rate"] < overall_wr and r["pnl_usd"] <= 0
    ]
    weak.sort(key=lambda r: (r["delta_vs_overall_wr"], r["pnl_usd"]))

    # Strengths = win_rate better than overall AND positive P&L
    strong = [
        {**r, "summary": (f"{r['bucket']} ({r['dimension']}) wins "
                          f"{r['win_rate']*100:.0f}% vs overall "
                          f"{overall_wr*100:.0f}% · ${r['pnl_usd']:+.2f}")}
        for r in rows
        if r["win_rate"] > overall_wr and r["pnl_usd"] > 0
    ]
    strong.sort(key=lambda r: (-r["delta_vs_overall_wr"], -r["pnl_usd"]))

    return {
        "trade_count": len(trades),
        "lookback_days": lookback_days,
        "overall": {
            "win_rate": round(overall_wr, 4),
            "total_pnl_usd": round(total_pnl, 2),
            "avg_pnl_usd": round(total_pnl / len(trades), 2),
            "wins": wins, "losses": losses,
        },
        "weaknesses": weak[:8],
        "strengths": strong[:5],
        "notes": notes,
    }
