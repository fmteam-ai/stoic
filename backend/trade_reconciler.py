"""Reconcile DB trades against the EA's actual list of open MT5 tickets.

A trade is "orphaned" when the broker no longer reports its ticket as open,
but STOIC still has it in a non-terminal state. Two flavours:

  • status="open"    — the original bug. Bot opened a position; SL hit at the
    broker; EA crashed / lost connection / missed the close report. Trade
    sits OPEN in STOIC forever, throwing off P&L and concurrent-trade caps.

  • status="pending" + close_requested=True — user clicked × CLOSE on the row.
    Backend set the trade to pending-close; EA was supposed to execute on MT5
    and ack with /bridge/report. EA never ack'd. Broker has already closed it
    (or it was never open) — STOIC should mirror reality.

Both are dangerous to leave hanging: anti-tilt counters miscount, drawdown
caps misfire, manual close buttons get stuck.
"""
from datetime import datetime, timezone
from bson import ObjectId
from database import get_db
import logging

logger = logging.getLogger("trade_reconciler")
from ws_manager import manager as ws_manager
from silent_failures import record_swallow


def _infer_close_reason(t: dict, exit_price) -> str:
    """iter-45 — when ghost-closing with an estimated exit, infer TP/SL hit
    from proximity to the trade's targets (~0.04% tolerance)."""
    try:
        exit_p = float(exit_price or 0)
        entry = float(t.get("entry_price") or 0)
        if exit_p > 0 and entry > 0:
            tol = max(entry * 0.0004, 0.3)
            sl = float(t.get("stop_loss") or 0)
            tp = float(t.get("tp3") or t.get("take_profit") or 0)
            if tp > 0 and abs(exit_p - tp) <= tol:
                return "take_profit_reconciled"
            if sl > 0 and abs(exit_p - sl) <= tol:
                return "stop_loss_reconciled"
    except Exception as _sw:  # noqa: BLE001
        record_swallow("reconciliation", "_infer_close_reason", _sw)
    return "broker_reconciled_estimated"


async def reconcile_account(account_id: str, open_tickets: list[int],
                             *, source: str = "heartbeat") -> dict:
    """Close any DB-open trade for `account_id` whose mt5_ticket is not in
    the EA's reported open list.

    Returns a summary dict for logging / API response.
    """
    db = get_db()
    open_set = {int(t) for t in (open_tickets or []) if t is not None}

    # GRACE WINDOW (iter-90): trades opened in the last 45 seconds are skipped.
    # The EA fills the order on broker, then sends a confirmation heartbeat —
    # there is a short window where the trade exists in DB with an mt5_ticket
    # but the next heartbeat hasn't included it in `positions` yet. Reaping
    # those would close the position in DB while the broker is still running it.
    from datetime import timedelta
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=45)).isoformat()

    # Orphan-able set:
    #   • status="open" with a real ticket (the original bug — SL hit, EA missed report)
    #   • status="pending" with close_requested=True and a real ticket (user clicked
    #     × CLOSE; EA never ack'd. Broker has already closed it — mirror that.)
    cursor = db.trades.find({
        "account_id": account_id,
        "$or": [
            {"status": "open", "mt5_ticket": {"$ne": None},
             "$or": [{"opened_at": {"$lt": cutoff_iso}}, {"opened_at": None}]},
            {"status": "pending", "close_requested": True, "mt5_ticket": {"$ne": None}},
        ],
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
        # pnl_unknown flags the row so loss-counting logic (anti-tilt,
        # optimizer) can tell "no data" apart from a real $0 breakeven.
        update = {
            "status": "closed",
            "closed_at": now_iso,
            "close_reason": t.get("close_reason") or f"broker_reconciled_{source}",
            "reconciled": True,
            "reconciled_at": now_iso,
        }
        if t.get("exit_price") is None:
            # iter-45 · Use the last heartbeat-snapshot P&L/price as the
            # ESTIMATED exit (better a close-to-real number than a dash).
            # The EA's deal-history sweep overwrites with exact broker
            # figures when/if the "out" deal arrives (pnl_estimated flag).
            live_pnl = t.get("live_pnl")
            live_price = t.get("live_price")
            if live_pnl is not None or live_price is not None:
                update["pnl"] = float(live_pnl or 0.0)
                update["exit_price"] = live_price
                update["pnl_estimated"] = True
                update["close_reason"] = t.get("close_reason") or _infer_close_reason(t, live_price)
            else:
                update["pnl_unknown"] = True
        await db.trades.update_one({"_id": t["_id"]}, {"$set": update})
        closed.append(str(t["_id"]))

        # WS push so the UI refreshes immediately
        user_id = t.get("user_id")
        if user_id:
            await ws_manager.broadcast(user_id, "trade_updated", {
                "trade_id": str(t["_id"]),
                **update,
            })

    # round 9 P0-01 — broker reconciliation watermark (feeds the authority
    # stability window: two fresh reconciliations before READY returns).
    try:
        await db.accounts.update_one(
            {"_id": ObjectId(account_id)},
            {"$set": {"last_reconciled_at": datetime.now(timezone.utc).isoformat(),
                      "last_reconciliation_source": source},
             "$inc": {"reconciliation_seq": 1}})
    except Exception as e:  # noqa: BLE001
        logger.warning("reconciliation watermark not stamped for %s: %s", account_id, e)

    return {
        "account_id": account_id,
        "open_tickets_reported": len(open_set),
        "candidates_in_db": len(candidates),
        "closed_count": len(closed),
        "closed_trade_ids": closed,
        "source": source,
    }


async def reconcile_user(user_id: str, *, force: bool = False) -> dict:
    """Run reconciliation across every account this user owns, using each
    account's last-known open_tickets stored on the heartbeat doc.

    Fallback for older EAs (no `open_tickets` field): if the most recent
    heartbeat reports `open_positions == 0`, we know unambiguously that the
    broker has zero open positions, so every DB-open trade on that account
    must be stale.

    Also sweeps "account-orphan" trades — pending/open trades attached to an
    account_id that no longer exists in the user's accounts (account was
    deleted while a position was still open). These can never be reconciled
    against a broker again, so we close them with reason="account_deleted".

    `force=True`: user has manually verified on the broker UI that positions
    are closed. Closes ALL DB-open + pending-close trades for every account,
    bypassing the heartbeat-freshness / open_positions guards. Also cancels
    never-executed pending-OPEN trades (mt5_ticket=None) which are stuck
    waiting for an offline EA.
    """
    db = get_db()
    cursor = db.accounts.find({"user_id": user_id})
    accounts = await cursor.to_list(length=20)
    valid_account_ids = {str(a["_id"]) for a in accounts}

    summaries = []

    # Phase 1: account-orphan sweep (trades whose account was deleted)
    orphan_cursor = db.trades.find({
        "user_id": user_id,
        "$or": [
            {"status": "open", "mt5_ticket": {"$ne": None}},
            {"status": "pending", "close_requested": True, "mt5_ticket": {"$ne": None}},
        ],
    })
    all_orphan_candidates = await orphan_cursor.to_list(length=200)
    deleted_account_orphans = [
        t for t in all_orphan_candidates
        if t.get("account_id") not in valid_account_ids
    ]
    if deleted_account_orphans:
        from ws_manager import manager as ws_manager
        now_iso = datetime.now(timezone.utc).isoformat()
        closed_ids = []
        for t in deleted_account_orphans:
            update = {
                "status": "closed",
                "closed_at": now_iso,
                "close_reason": "account_deleted",
                "reconciled": True,
                "reconciled_at": now_iso,
            }
            await db.trades.update_one({"_id": t["_id"]}, {"$set": update})
            closed_ids.append(str(t["_id"]))
            await ws_manager.broadcast(user_id, "trade_updated", {
                "trade_id": str(t["_id"]),
                **update,
            })
        summaries.append({
            "account_id": "<deleted>",
            "label": "Deleted account orphans",
            "closed_count": len(closed_ids),
            "closed_trade_ids": closed_ids,
            "source": "account_deleted_sweep",
        })

    # Phase 2: per-account reconciliation against live broker state
    for acc in accounts:
        if force:
            # When the EA *is* reporting tickets, those are the ground truth —
            # Force Sync must respect them and only close trades NOT in the EA's
            # open list. The previous version passed `[]` here, which silently
            # wiped every DB-open trade regardless of what the broker said and
            # repeatedly burned live positions. Force Sync now means
            # "reconcile aggressively against the broker's live ticket list",
            # NOT "close everything no matter what".
            ea_open_positions = int(acc.get("open_positions") or 0)
            ea_tickets = acc.get("open_tickets")  # None on legacy EAs
            knows_tickets = ea_tickets is not None

            if ea_open_positions > 0 and not knows_tickets:
                # Legacy EA + broker says positions open → refuse (unchanged guard).
                summaries.append({
                    "account_id": str(acc["_id"]),
                    "label": acc.get("label"),
                    "skipped": True,
                    "reason": (
                        f"Force Sync refused — EA reports {ea_open_positions} open positions "
                        "but is on a legacy version that can't list tickets. Upgrade the EA "
                        "to v1.22+ (or v1.25 for full sync) before force-closing."
                    ),
                    "ea_version_hint": "Re-install EA from Accounts → Download EA, then F7 to compile, then re-attach to chart.",
                })
                continue

            # Modern EA (knows_tickets=True) → use the real list. Legacy EA
            # with zero positions → still safe to pass [].
            authoritative_tickets = ea_tickets if knows_tickets else []
            summary = await reconcile_account(
                str(acc["_id"]), authoritative_tickets, source="manual_force",
            )
            # Also cancel never-executed OPEN/pending trades that never got a
            # broker ticket. Two states qualify:
            #   • status="pending" + mt5_ticket=None  (never fired to broker)
            #   • status="open"    + mt5_ticket=None  (bug state — bot recorded
            #     the fill locally but broker never confirmed with a ticket.
            #     The trade sits open forever, consuming risk-cap slots. Force
            #     Sync deliberately reaps this class of orphan since there is
            #     no broker-side position to reconcile against.)
            cancel_cursor = db.trades.find({
                "account_id": str(acc["_id"]),
                "status": {"$in": ["pending", "open"]},
                "mt5_ticket": None,
            })
            cancelled = []
            now_iso = datetime.now(timezone.utc).isoformat()
            async for t in cancel_cursor:
                is_open = t.get("status") == "open"
                update = {
                    "status": "closed" if is_open else "cancelled",
                    "closed_at": now_iso,
                    "close_reason": ("force_reaped_ticketless_open"
                                     if is_open else "force_cancelled_never_filled"),
                    "reconciled": True,
                    "reconciled_at": now_iso,
                }
                await db.trades.update_one({"_id": t["_id"]}, {"$set": update})
                cancelled.append(str(t["_id"]))
            summary["cancelled_pending_opens"] = len(cancelled)
            summary["closed_count"] = summary.get("closed_count", 0) + len(cancelled)
            summary["label"] = acc.get("label")
            summary["forced"] = True
            summaries.append(summary)
            continue

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
