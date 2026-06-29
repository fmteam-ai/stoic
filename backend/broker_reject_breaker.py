"""Broker-rejection circuit breaker (iter-71).

Stops the bleeding when a broker keeps rejecting our orders with the same
MT5 retcode. Most commonly:

    10013 (INVALID_REQUEST)   — symbol name mismatch (broker uses ".x"/".raw"
                                /"pro" suffix), filling mode unsupported, or
                                symbol not in MarketWatch.
    10016 (INVALID_STOPS)     — SL/TP inside broker's "stops level" buffer.
    10014 (INVALID_VOLUME)    — lot size below broker minimum.
    10027 (AUTOTRADING_OFF)   — broker-side autotrading disabled.

Once we see N consecutive same-retcode failures within a short window, we
flip the account to `trading_blocked: True` with a clear `block_reason`.
The bot runner skips blocked accounts entirely. The user clears the flag
via POST /api/accounts/{id}/unblock once they've fixed the underlying
issue (EA recompile, broker symbol config, etc.).
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

logger = logging.getLogger("broker_reject_breaker")

# How many same-retcode failures inside the window trip the breaker.
TRIP_AFTER = 3
# Lookback window for "consecutive" — failures older than this don't count.
WINDOW_MINUTES = 30

# Map retcodes to human-readable causes + remediation hints.
_RETCODE_HINTS = {
    "10013": ("INVALID_REQUEST",
              "Symbol name mismatch (broker uses suffix like .x/.raw/pro) "
              "or symbol not enabled in MarketWatch. Recompile EA v1.29+ "
              "(auto-detects suffixes) or set the correct symbol_suffix on "
              "this account."),
    "10016": ("INVALID_STOPS",
              "Broker rejects SL/TP placement — too close to entry. Widen "
              "your risk profile (Medium → Aggressive) or switch broker."),
    "10014": ("INVALID_VOLUME",
              "Lot size below broker minimum. Increase account equity or "
              "switch to a broker with smaller minimum lot (e.g. cent accounts)."),
    "10018": ("MARKET_CLOSED",
              "Symbol is not tradable right now. Check broker market hours."),
    "10019": ("NO_MONEY",
              "Insufficient margin. Reduce risk_pct or deposit more funds."),
    "10027": ("AUTOTRADING_OFF",
              "Broker server has autotrading disabled for your account. "
              "Contact your broker to enable algorithmic trading."),
    "SYMBOL_NOT_FOUND": ("SYMBOL_NOT_FOUND",
                         "EA v1.29+ probed bare symbol + 18 common suffixes "
                         "and none matched the broker's instrument list. "
                         "Check the symbol exists in MarketWatch with the "
                         "exact name your broker uses."),
}


def _extract_retcode(error: str) -> Optional[str]:
    """Parse the failure tag from the EA's error field.

    Recognises:
      • retcode=10013   (MT5 numeric retcode)
      • symbol_not_found:XAUUSD   (v1.29 explicit tag)
    """
    if not error:
        return None
    if error.startswith("symbol_not_found"):
        return "SYMBOL_NOT_FOUND"
    if "retcode=" not in error:
        return None
    code = error.split("retcode=", 1)[1].strip()
    code_digits = ""
    for c in code:
        if c.isdigit():
            code_digits += c
        else:
            break
    return code_digits or None


async def evaluate_account(db, account: dict) -> dict:
    """Check the account's recent failure pattern; flip `trading_blocked`
    on the DB if the breaker trips.

    Returns:
        {
          "blocked": bool,
          "tripped_this_call": bool,
          "retcode": str | None,
          "label": str | None,
          "hint": str | None,
          "consecutive_failures": int,
          "block_reason": str | None,
        }
    """
    account_id = str(account["_id"])
    now = datetime.now(timezone.utc)

    # Already blocked? Just return current state without re-checking.
    if account.get("trading_blocked"):
        return {
            "blocked": True,
            "tripped_this_call": False,
            "retcode": account.get("block_retcode"),
            "label": account.get("block_retcode_label"),
            "hint": account.get("block_hint"),
            "consecutive_failures": int(account.get("block_failure_count") or 0),
            "block_reason": account.get("block_reason"),
        }

    since = (now - timedelta(minutes=WINDOW_MINUTES)).isoformat()
    # iter-74b · If the user manually cleared the block (via unblock endpoint
    # OR via symbol_suffix update), ignore any failures that pre-date that
    # action — those are stale evidence the user already addressed. Without
    # this, the breaker re-trips the next tick on the same old failures.
    unblocked_at = account.get("unblocked_at")
    if unblocked_at and unblocked_at > since:
        since = unblocked_at
    cursor = db.trades.find(
        {
            "account_id": account_id,
            "status": "failed",
            "opened_at": {"$gte": since},
            "error": {"$exists": True, "$ne": None},
        },
        projection={"error": 1, "opened_at": 1, "symbol": 1},
        sort=[("opened_at", -1)],
        limit=TRIP_AFTER * 3,
    )
    recent = [t async for t in cursor]
    if len(recent) < TRIP_AFTER:
        return {"blocked": False, "tripped_this_call": False,
                "retcode": None, "label": None, "hint": None,
                "consecutive_failures": len(recent), "block_reason": None}

    # Count failures by retcode within the window; trip when same code
    # accounts for ≥ TRIP_AFTER of the most recent failures.
    last_n = recent[:TRIP_AFTER]
    codes = [_extract_retcode(t.get("error", "")) for t in last_n]
    if not all(c == codes[0] and c is not None for c in codes):
        return {"blocked": False, "tripped_this_call": False,
                "retcode": None, "label": None, "hint": None,
                "consecutive_failures": len(recent), "block_reason": None}

    retcode = codes[0]
    label, hint = _RETCODE_HINTS.get(retcode, ("UNKNOWN_RETCODE",
                                                "Repeated broker rejection — check MT5 Experts tab."))
    reason = (f"{label} ({retcode}) ×{TRIP_AFTER} on {account.get('label')} "
              f"in {WINDOW_MINUTES}min — trading auto-halted to stop the bleed.")

    await db.accounts.update_one(
        {"_id": account["_id"]},
        {"$set": {
            "trading_blocked": True,
            "block_reason": reason,
            "block_retcode": retcode,
            "block_retcode_label": label,
            "block_hint": hint,
            "block_failure_count": TRIP_AFTER,
            "blocked_at": now.isoformat(),
        }},
    )
    logger.warning("broker-reject breaker TRIPPED — account=%s retcode=%s",
                   account.get("label"), retcode)
    return {
        "blocked": True,
        "tripped_this_call": True,
        "retcode": retcode,
        "label": label,
        "hint": hint,
        "consecutive_failures": TRIP_AFTER,
        "block_reason": reason,
    }


async def unblock_account(db, account_id) -> bool:
    """Manually clear the trading_blocked flag (called from API)."""
    result = await db.accounts.update_one(
        {"_id": account_id},
        {"$set": {
            "trading_blocked": False,
            "unblocked_at": datetime.now(timezone.utc).isoformat(),
        }, "$unset": {
            "block_reason": "", "block_retcode": "", "block_retcode_label": "",
            "block_hint": "", "block_failure_count": "",
        }},
    )
    return result.modified_count > 0
