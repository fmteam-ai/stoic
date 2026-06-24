"""Per-account performance comparison — side-by-side aggregates.

Pulls closed/open trades grouped by `account_id`, joins each row with the
account doc (label / broker / mode) and the per-account or default bot_config
(active_preset, risk_level, max_lot_size). Output drives the dashboard's
A/B comparison widget so the user can see at a glance which account is
actually profitable.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

from database import get_db


def _parse_iso(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


async def per_account_stats(user_id: str) -> dict:
    db = get_db()
    accounts = await db.accounts.find({"user_id": user_id}).to_list(length=50)
    cfgs = await db.bot_configs.find({"user_id": user_id}).to_list(length=100)
    # Index configs: account_id -> cfg, plus the user's default (account_id None)
    cfg_by_acc: dict = {}
    default_cfg: dict = {}
    for c in cfgs:
        if c.get("account_id"):
            cfg_by_acc[c["account_id"]] = c
        else:
            default_cfg = c

    trades = await db.trades.find(
        {"user_id": user_id},
        {
            "account_id": 1, "status": 1, "pnl": 1,
            "closed_at": 1, "opened_at": 1, "symbol": 1,
            "lot_size": 1, "close_reason": 1, "action": 1,
        },
    ).to_list(length=10000)

    now = datetime.now(timezone.utc)
    cutoff_30d = now - timedelta(days=30)

    # Group raw stats per account_id
    grouped: dict = {}
    for t in trades:
        aid = t.get("account_id") or "unassigned"
        bucket = grouped.setdefault(aid, {
            "closed": 0, "wins": 0, "losses": 0,
            "total_pnl": 0.0, "pnl_30d": 0.0,
            "gross_win": 0.0, "gross_loss": 0.0,
            "open": 0, "pending": 0,
            "last_trade_at": None,
        })
        status = t.get("status")
        if status in ("pending",):
            bucket["pending"] += 1
            continue
        if status == "open":
            bucket["open"] += 1
            continue
        if status != "closed":
            continue   # cancelled / failed — skip

        bucket["closed"] += 1
        pnl = float(t.get("pnl") or 0)
        bucket["total_pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
            bucket["gross_win"] += pnl
        elif pnl < 0:
            bucket["losses"] += 1
            bucket["gross_loss"] += abs(pnl)
        # 30-day rolling P&L
        closed_at = _parse_iso(t.get("closed_at"))
        if closed_at and closed_at >= cutoff_30d:
            bucket["pnl_30d"] += pnl
        if closed_at and (bucket["last_trade_at"] is None
                          or closed_at > bucket["last_trade_at"]):
            bucket["last_trade_at"] = closed_at

    # Build rows with derived metrics + account/config metadata.
    rows = []
    for acc in accounts:
        aid = str(acc["_id"])
        b = grouped.get(aid, {})
        closed = int(b.get("closed", 0))
        wins = int(b.get("wins", 0))
        gross_win = float(b.get("gross_win", 0.0))
        gross_loss = float(b.get("gross_loss", 0.0))
        win_rate = round((wins / closed) * 100, 1) if closed > 0 else 0.0
        profit_factor = round(gross_win / gross_loss, 2) if gross_loss > 0 else None
        avg_pnl = round(b.get("total_pnl", 0.0) / closed, 4) if closed > 0 else 0.0
        cfg = cfg_by_acc.get(aid) or default_cfg
        last_iso = b.get("last_trade_at")
        rows.append({
            "account_id": aid,
            "label": acc.get("label") or acc.get("account_number") or aid[-6:],
            "broker": acc.get("broker") or "—",
            "mode": acc.get("mode") or "live",
            "balance": float(acc.get("balance") or 0.0),
            "equity": float(acc.get("equity") or 0.0),
            "base_currency": acc.get("base_currency") or "USD",
            # Performance
            "closed_trades": closed,
            "open_trades": int(b.get("open", 0)),
            "pending_trades": int(b.get("pending", 0)),
            "wins": wins,
            "losses": int(b.get("losses", 0)),
            "win_rate": win_rate,
            "total_pnl": round(b.get("total_pnl", 0.0), 2),
            "pnl_30d": round(b.get("pnl_30d", 0.0), 2),
            "avg_pnl": avg_pnl,
            "profit_factor": profit_factor,
            "last_trade_at": last_iso.isoformat() if last_iso else None,
            # Bot config snapshot
            "active_preset": (cfg or {}).get("active_preset"),
            "risk_level": (cfg or {}).get("risk_level", "medium"),
            "max_lot_size": float((cfg or {}).get("max_lot_size") or 0.0),
            "bot_active": bool((cfg or {}).get("active")),
            "has_override": aid in cfg_by_acc,
        })

    # Sort by total_pnl desc so the leader is row 0 and the UI can mark it.
    rows.sort(key=lambda r: r["total_pnl"], reverse=True)

    # Aggregate roll-up across all accounts (handy header)
    totals = {
        "accounts": len(rows),
        "closed_trades": sum(r["closed_trades"] for r in rows),
        "open_trades": sum(r["open_trades"] for r in rows),
        "total_pnl": round(sum(r["total_pnl"] for r in rows), 2),
        "pnl_30d": round(sum(r["pnl_30d"] for r in rows), 2),
    }
    return {"accounts": rows, "totals": totals}
