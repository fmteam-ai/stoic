"""Insights aggregation routes — Weekly AI Digest and related analytical
summaries the dashboard surfaces to users.
"""
from datetime import datetime, timedelta, timezone
import re
from typing import Optional

from fastapi import APIRouter, Depends
from bson import ObjectId

from auth import get_current_user
from database import get_db

router = APIRouter(prefix="/insights", tags=["insights"])


def _safe_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _summarise_hold_reasons(reasonings: list[str]) -> list[dict]:
    """Bucket bot HOLD reasons into 4 canonical categories + count occurrences."""
    buckets = {
        "Noisy entropy": 0,
        "Market closed": 0,
        "Macro freeze": 0,
        "Regime CHOP": 0,
        "Other": 0,
    }
    for r in reasonings:
        rl = (r or "").lower()
        if not rl:
            continue
        if "market closed" in rl:
            buckets["Market closed"] += 1
        elif "noise" in rl or "entropy" in rl:
            buckets["Noisy entropy"] += 1
        elif "macro" in rl or "freeze" in rl:
            buckets["Macro freeze"] += 1
        elif "chop" in rl:
            buckets["Regime CHOP"] += 1
        else:
            buckets["Other"] += 1
    return [{"label": k, "count": v} for k, v in buckets.items() if v > 0]


def _suggest_action(stats: dict) -> str:
    """Rule-based suggestion line — friendly, actionable, never alarmist."""
    win_rate = stats["win_rate"]
    pnl = stats["pnl_total"]
    trades = stats["trades"]
    heals = stats["auto_heals"]

    if trades == 0:
        return ("No trades closed this week. Market entropy and weekend closures kept "
                "the bot patient — consider adding BTCUSD to widen the signal surface.")
    if pnl < 0 and win_rate < 35:
        return ("Win rate below 35% and net negative — Loss Lab is the next stop. "
                "Auto-heal will tighten guardrails automatically on the next cycle.")
    if pnl > 0 and win_rate >= 60:
        return ("Strong week — win rate ≥ 60% with positive P&L. Consider unlocking "
                "the next risk tier or increasing per-trade sizing slightly.")
    if heals >= 3:
        return ("Auto-heal triggered ≥3 times — the bot is actively retuning. "
                "Hold the current configuration for one more week before manual changes.")
    return ("Steady week — keep the current configuration. Add more symbols to the "
            "watchlist if you want higher trade frequency.")


@router.get("/weekly-digest")
async def weekly_digest(days: int = 7, user=Depends(get_current_user)):
    """Aggregated 7-day (or N-day) recap of the user's bot activity.

    Powers the dashboard's Weekly AI Digest widget. Returns:
      - trade stats (count, win rate, P&L, best, worst)
      - auto-heal action breakdown by kind
      - top HOLD reasons during the window
      - a suggested next action (rule-based)
    """
    db = get_db()
    days = max(1, min(int(days or 7), 30))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since.isoformat()
    since_date_iso = since.date().isoformat()

    # ----- Trades closed in window -----
    trade_q = {
        "user_id": user["id"],
        "status": "closed",
        "closed_at": {"$gte": since_date_iso},
    }
    trades = await db.trades.find(trade_q).to_list(length=5000)
    n_trades = len(trades)
    wins = [t for t in trades if (t.get("pnl") or 0) > 0]
    losses = [t for t in trades if (t.get("pnl") or 0) < 0]
    n_wins, n_losses = len(wins), len(losses)
    win_rate = round((n_wins / n_trades * 100), 1) if n_trades else 0.0
    pnl_total = round(sum((t.get("pnl") or 0) for t in trades), 2)
    avg_win = round(sum((t.get("pnl") or 0) for t in wins) / n_wins, 2) if wins else 0.0
    avg_loss = round(sum((t.get("pnl") or 0) for t in losses) / n_losses, 2) if losses else 0.0

    def _trade_slim(t: dict) -> dict:
        return {
            "id": str(t.get("_id")),
            "symbol": t.get("symbol"),
            "action": t.get("action"),
            "pnl": round(float(t.get("pnl") or 0), 2),
            "closed_at": t.get("closed_at"),
        }

    best_trade = _trade_slim(max(trades, key=lambda x: x.get("pnl") or 0)) if trades else None
    worst_trade = _trade_slim(min(trades, key=lambda x: x.get("pnl") or 0)) if trades else None

    # ----- Auto-heal actions in window -----
    heal_q = {"user_id": user["id"], "ts": {"$gte": since_iso}}
    heals = await db.auto_heal_actions.find(heal_q).to_list(length=1000)
    heal_buckets: dict = {}
    for h in heals:
        k = h.get("kind") or "unknown"
        heal_buckets[k] = heal_buckets.get(k, 0) + 1
    heal_breakdown = [{"kind": k, "count": v} for k, v in
                      sorted(heal_buckets.items(), key=lambda x: -x[1])]

    # ----- HOLD reasons in window -----
    signal_q = {"action": "HOLD", "created_at": {"$gte": since_iso}}
    signals = await db.signals.find(signal_q).limit(2000).to_list(length=2000)
    hold_reasons = _summarise_hold_reasons([s.get("reasoning", "") for s in signals])

    stats = {
        "trades": n_trades,
        "wins": n_wins,
        "losses": n_losses,
        "win_rate": win_rate,
        "pnl_total": pnl_total,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "auto_heals": len(heals),
    }

    return {
        "window_days": days,
        "window_start": since_iso,
        "stats": stats,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "auto_heal_breakdown": heal_breakdown,
        "hold_reasons": hold_reasons,
        "suggested_action": _suggest_action(stats),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
