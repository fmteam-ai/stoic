"""Integrity Snapshot — passive monitoring of broker↔DB drift.

For every account the user owns, compares:
  • broker reported open positions  (from heartbeat)
  • DB-open + DB-pending-close trades

Mismatches surface as orphan candidates that SYNC WITH BROKER would close.
This endpoint is read-only — it never modifies state. Used by the Dashboard
"Last broker sync · Xs ago · N mismatches" widget.
"""
from datetime import datetime, timezone
from database import get_db


def _seconds_since_iso(iso_str: str | None) -> int | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return None


def _classify(*, last_sync_age: int | None, mismatches: int, has_account: bool) -> str:
    if not has_account:
        return "no_account"
    if last_sync_age is None or last_sync_age > 60:
        return "stale"
    if mismatches > 0:
        return "drift"
    return "ok"


async def snapshot(user_id: str) -> dict:
    db = get_db()
    accounts = await db.accounts.find({"user_id": user_id}).to_list(length=20)
    valid_account_ids = {str(a["_id"]) for a in accounts}

    per_account = []
    total_mismatches = 0
    worst_state = "ok"
    state_rank = {"ok": 0, "drift": 1, "stale": 2, "no_account": 3}

    for a in accounts:
        aid = str(a["_id"])
        broker_open = a.get("open_positions", 0) or 0
        # Tickets the EA is reporting as open (v1.22+); falls back to count match
        open_tickets = a.get("open_tickets")

        db_open = await db.trades.count_documents({
            "account_id": aid, "status": "open", "mt5_ticket": {"$ne": None},
        })
        db_pending_close = await db.trades.count_documents({
            "account_id": aid, "status": "pending", "close_requested": True,
            "mt5_ticket": {"$ne": None},
        })
        db_total = db_open + db_pending_close

        # Mismatch: anything DB has that the broker doesn't, OR vice versa
        if open_tickets is not None:
            # Precise comparison via ticket sets
            db_tickets = set()
            cursor = db.trades.find({
                "account_id": aid,
                "$or": [
                    {"status": "open", "mt5_ticket": {"$ne": None}},
                    {"status": "pending", "close_requested": True, "mt5_ticket": {"$ne": None}},
                ],
            }, {"mt5_ticket": 1})
            async for t in cursor:
                db_tickets.add(int(t["mt5_ticket"]))
            broker_tickets = {int(t) for t in open_tickets if t is not None}
            mismatches = len(db_tickets ^ broker_tickets)  # symmetric diff
        else:
            # Approximate via count diff (older EA)
            mismatches = abs(db_total - broker_open)

        last_sync_age = _seconds_since_iso(a.get("last_heartbeat"))
        state = _classify(
            last_sync_age=last_sync_age, mismatches=mismatches, has_account=True,
        )
        if state_rank[state] > state_rank[worst_state]:
            worst_state = state

        total_mismatches += mismatches
        per_account.append({
            "account_id": aid,
            "label": a.get("label"),
            "broker": a.get("broker"),
            "broker_open": broker_open,
            "db_open": db_open,
            "db_pending_close": db_pending_close,
            "mismatches": mismatches,
            "last_sync_age_seconds": last_sync_age,
            "state": state,
            "ea_supports_tickets": open_tickets is not None,
        })

    # Account-orphan trades (account_id no longer exists)
    orphan_count = await db.trades.count_documents({
        "user_id": user_id,
        "$or": [
            {"status": "open", "mt5_ticket": {"$ne": None}},
            {"status": "pending", "close_requested": True, "mt5_ticket": {"$ne": None}},
        ],
        "account_id": {"$nin": list(valid_account_ids)},
    })

    if orphan_count > 0:
        worst_state = "drift"
        total_mismatches += orphan_count

    if not accounts:
        worst_state = "no_account"

    return {
        "state": worst_state,                 # 'ok' | 'drift' | 'stale' | 'no_account'
        "total_mismatches": total_mismatches,
        "deleted_account_orphans": orphan_count,
        "accounts": per_account,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
