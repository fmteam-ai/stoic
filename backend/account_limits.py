"""Per-user limits for accounts/brokers/bots.

Single source of truth. Bump these constants to grant more capacity globally,
or override per-user via the `account_limits` field on the user document
(future: tie to subscription tier).
"""
from datetime import datetime, timezone

MAX_BROKERS_PER_USER = 5
MAX_ACCOUNTS_PER_BROKER = 3
PAPER_BROKER_TAG = "INTERNAL_PAPER"  # paper accounts skip limit checks


def _norm(name: str) -> str:
    return (name or "").strip().lower()


async def get_broker_breakdown(db, user_id: str) -> dict:
    """Return how the user's *live* accounts are distributed across brokers.

    Paper accounts are excluded from limit accounting entirely so users can
    always sandbox-test without consuming broker slots.

    Returns:
      {
        "max_brokers": int,
        "max_accounts_per_broker": int,
        "brokers_used": int,
        "breakdown": [
          {"broker": "RoboForex", "count": 2, "remaining_slots": 1}, ...
        ],
        "total_live_accounts": int,
      }
    """
    cursor = db.accounts.find({"user_id": user_id, "mode": {"$ne": "paper"}})
    accs = await cursor.to_list(length=200)
    counts: dict[str, dict] = {}
    for a in accs:
        broker = (a.get("broker") or "").strip()
        if not broker or broker == PAPER_BROKER_TAG:
            continue
        key = _norm(broker)
        if key not in counts:
            counts[key] = {"broker": broker, "count": 0}
        counts[key]["count"] += 1
    breakdown = [
        {
            "broker": v["broker"],
            "count": v["count"],
            "remaining_slots": max(0, MAX_ACCOUNTS_PER_BROKER - v["count"]),
        }
        for v in sorted(counts.values(), key=lambda x: x["broker"].lower())
    ]
    return {
        "max_brokers": MAX_BROKERS_PER_USER,
        "max_accounts_per_broker": MAX_ACCOUNTS_PER_BROKER,
        "brokers_used": len(counts),
        "breakdown": breakdown,
        "total_live_accounts": sum(v["count"] for v in counts.values()),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


async def check_can_add_live_account(db, user_id: str, broker: str) -> tuple[bool, str | None]:
    """Return (allowed, error_message). error_message is None when allowed."""
    broker = (broker or "").strip()
    if not broker:
        return False, "Broker name is required for live accounts"
    key = _norm(broker)
    info = await get_broker_breakdown(db, user_id)
    existing = {_norm(b["broker"]): b for b in info["breakdown"]}
    is_new_broker = key not in existing
    if is_new_broker and info["brokers_used"] >= MAX_BROKERS_PER_USER:
        return False, (
            f"Broker limit reached — you can connect up to {MAX_BROKERS_PER_USER} "
            f"different brokers. Remove an existing broker or contact support to extend."
        )
    if not is_new_broker and existing[key]["count"] >= MAX_ACCOUNTS_PER_BROKER:
        return False, (
            f"Account limit reached for {broker} — up to {MAX_ACCOUNTS_PER_BROKER} "
            f"accounts per broker. Remove one or pick a different broker."
        )
    return True, None
