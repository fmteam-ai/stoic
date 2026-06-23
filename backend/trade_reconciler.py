"""Reconcile DB-open trades against the EA's actual list of open MT5 tickets.

A trade is "orphaned" when:
  • DB has it as status="open" with a non-null mt5_ticket
  • Heartbeat says the broker no longer has that ticket open

The most common cause is a missed `/bridge/report` POST when MT5 fired a
stop-loss / take-profit. EA may have crashed, restarted, or had a network blip
at the moment of close. Without reconciliation, the trade stays "open" in the
UI forever, throwing off P&L, anti-tilt counters, and concurrent-trade caps.
"""
from datetime import datetime, timezone
from bson import ObjectId
from database import get_db
from ws_manager import manager as ws_manager


async def reconcile_account(account_id: str, open_tickets: list[int],
                             *, source: str = "heartbeat") -> dict:
    """Close any DB-open trade for `account_id` whose mt5_ticket is not in
    the EA's reported open list.

    Returns a summary dict for logging / API response.
    """
    db = get_db()
    open_set = {int(t) for t in (open_tickets or []) if t is not None}

    # Find all DB-open trades on this account that have a real ticket
    cursor = db.trades.find({
        "account_id": account_id,
        "status": "open",
        "mt5_ticket": {"$ne": None},
    })
    candidates = await cursor.to_list(length=200)

    closed = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for t in candidates:
        ticket = t.get("mt5_ticket")
        if ticket is None or int(ticket) in open_set:
            continue   # still open at the broker, skip

        # ORPHAN — broker says it's no longer open. Mark it closed.
        # We can't infer exit_price reliably here (no quote in heartbeat), so
        # leave exit_price/pnl untouched if the EA never reported them.
        update = {
            "status": "closed",
            "closed_at": now_iso,
            "close_reason": t.get("close_reason") or f"broker_reconciled_{source}",
            "reconciled": True,
            "reconciled_at": now_iso,
        }
        await db.trades.update_one({"_id": t["_id"]}, {"$set": update})
        closed.append(str(t["_id"]))

        # WS push so the UI refreshes immediately
        user_id = t.get("user_id")
        if user_id:
            await ws_manager.broadcast(user_id, "trade_updated", {
                "trade_id": str(t["_id"]),
                **update,
            })

    return {
        "account_id": account_id,
        "open_tickets_reported": len(open_set),
        "candidates_in_db": len(candidates),
        "closed_count": len(closed),
        "closed_trade_ids": closed,
        "source": source,
    }


async def reconcile_user(user_id: str) -> dict:
    """Run reconciliation across every account this user owns, using each
    account's last-known open_tickets stored on the heartbeat doc.

    Fallback for older EAs (no `open_tickets` field): if the most recent
    heartbeat reports `open_positions == 0`, we know unambiguously that the
    broker has zero open positions, so every DB-open trade on that account
    must be stale.
    """
    db = get_db()
    cursor = db.accounts.find({"user_id": user_id})
    accounts = await cursor.to_list(length=20)
    summaries = []
    for acc in accounts:
        tickets = acc.get("open_tickets")
        if tickets is None:
            # Old EA. Fall back to count-based reconciliation when it's safe.
            if (acc.get("open_positions") or 0) == 0 and acc.get("last_heartbeat"):
                summary = await reconcile_account(
                    str(acc["_id"]), [], source="manual_count_fallback",
                )
                summary["label"] = acc.get("label")
                summary["fallback"] = "open_positions==0"
                summaries.append(summary)
            else:
                summaries.append({
                    "account_id": str(acc["_id"]),
                    "label": acc.get("label"),
                    "skipped": True,
                    "reason": "ea_too_old_and_positions_nonzero",
                    "ea_version_hint": "Update EA to v1.22+ for ticket-level reconciliation",
                })
            continue
        summary = await reconcile_account(
            str(acc["_id"]), tickets, source="manual",
        )
        summary["label"] = acc.get("label")
        summaries.append(summary)
    total_closed = sum(s.get("closed_count", 0) for s in summaries)
    return {"accounts": summaries, "total_closed": total_closed}
