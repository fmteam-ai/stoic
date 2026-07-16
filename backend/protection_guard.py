"""Protection-recovery state machine (round 8 critical item).

Detecting an unprotected position is not enough for an autonomous system —
this guard AUTOMATICALLY remediates every open trade flagged
protection_missing=True:

    PROTECTION_UNKNOWN
      → EMERGENCY_STOP_PENDING   (queue MODIFY_SL with a budget-based stop)
      → RESOLVED                 (EA confirmed a protective stop)
      → EMERGENCY_CLOSE_PENDING  (stop could not be placed → FULL_CLOSE)

While any unprotected position exists on an account:
  • new scalp entries are blocked (in-memory flag in scalp.engine)
  • the unknown position is counted at a conservative max-risk amount in
    the account stop-risk budget
  • the user is alerted via the notifications feed
"""

import logging
from datetime import datetime, timezone

from pip_utils import pip_value_usd_per_lot

logger = logging.getLogger(__name__)

MAX_STOP_ATTEMPTS = 3          # MODIFY_SL retries before FULL_CLOSE
EMERGENCY_RISK_PCT = 0.5       # emergency stop sized to 0.5% of equity
CONSERVATIVE_RISK_PCT = 0.5    # unknown position counted at 0.5% equity
FALLBACK_PRICE_PCT = 0.5       # stop distance cap: 0.5% of entry price
CLOSE_ACK_ESCALATION_SEC = 120  # re-queue FULL_CLOSE if no EA ack by then
CLOSE_ALERT_ATTEMPTS = 3        # operational alert after this many retries


async def find_account(db, account_id: str) -> tuple:
    """Round 11 item 7 — centralized account lookup with an EXPLICIT reason.

    Returns (account_or_None, reason) where reason is one of:
    'ok', 'invalid_id', 'missing', 'db_error'. Tries ObjectId first, then a
    raw string _id, so a valid account stored under another identifier
    format is never mistaken for a missing one."""
    if not account_id:
        return None, "invalid_id"
    oid = None
    try:
        from bson import ObjectId
        oid = ObjectId(account_id)
    except Exception:  # noqa: BLE001 — not a valid ObjectId string
        oid = None
    try:
        acc = None
        if oid is not None:
            acc = await db.accounts.find_one({"_id": oid})
        if acc is None:
            acc = await db.accounts.find_one({"_id": account_id})
        if acc is None:
            return None, ("missing" if oid is not None else "invalid_id")
        return acc, "ok"
    except Exception as e:  # noqa: BLE001
        logger.error("account lookup failed for %s: %s", account_id, e)
        return None, "db_error"


def apply_protection_ack(trade: dict, success: bool, new_sl=None,
                         error: str | None = None,
                         now_iso: str | None = None) -> dict:
    """Round 11 item 9 — dependency-free protection-acknowledgement policy
    (no FastAPI/BSON/Mongo imports). Returns the fields to $set on the
    trade document for the PROTECTION portion of an EA modification ack.

    - success + new_sl while EMERGENCY_STOP_PENDING → broker CONFIRMED the
      protective stop: record confirmed value, resolve the emergency.
    - failure → record the EA error; an emergency trade returns to
      PROTECTION_UNKNOWN so the guard retries/escalates. Never assume
      protection.
    """
    update: dict = {}
    ts = now_iso or datetime.now(timezone.utc).isoformat()
    if success and new_sl is not None:
        if trade.get("protection_state") == "EMERGENCY_STOP_PENDING":
            update["confirmed_stop_loss"] = float(new_sl)
            update["protection_state"] = "RESOLVED"
            update["protection_missing"] = False
            update["protection_resolved_at"] = ts
    elif not success:
        update["last_modification_error"] = error or "unknown EA error"
        if trade.get("protection_state") == "EMERGENCY_STOP_PENDING":
            update["protection_state"] = "PROTECTION_UNKNOWN"
    return update


def calculate_emergency_stop(entry: float, direction: str, lot: float,
                             symbol: str, equity: float) -> float | None:
    """Budget-based protective stop: distance = risk budget / (lot × pip
    value), capped at FALLBACK_PRICE_PCT of the entry price. Round 9 item 5:
    the budget is NEVER silently inflated — if equity is too small to place
    a meaningful stop, return None and let the guard escalate to an
    emergency full close instead."""
    if not entry or not lot:
        return None
    from pip_utils import pip_size
    pv = pip_value_usd_per_lot(symbol, None) or 10.0
    pip = pip_size(symbol) or 0.0001
    budget = (equity or 0) * EMERGENCY_RISK_PCT / 100.0
    if budget <= 0:
        return None
    dist_pips = budget / max(lot * pv, 1e-9)
    if dist_pips < 1.0:
        return None                      # tighter than 1 pip is not a stop
    dist_px = min(dist_pips * pip, entry * FALLBACK_PRICE_PCT / 100.0)
    if dist_px <= 0:
        return None
    sl = entry - dist_px if (direction or "BUY").upper() == "BUY" else entry + dist_px
    return round(sl, 5)


async def repair_unprotected_positions(db) -> dict:
    """One sweep of the recovery state machine. Idempotent; safe to run
    every reconcile interval."""
    now_iso = datetime.now(timezone.utc).isoformat()
    trades = await db.trades.find(
        {"status": "open", "protection_missing": True}).to_list(50)
    resolved = stops_queued = closes_queued = 0
    accounts_blocked: set = set()
    for tr in trades:
        tid = tr["_id"]
        account_id = str(tr.get("account_id") or "")
        # EA reported a protective stop since the flag was raised → RESOLVED.
        # Round 9 item 8 — the resolving stop value is recorded as the
        # broker-confirmed protection level, not just assumed.
        if float(tr.get("stop_loss") or 0) != 0.0:
            await db.trades.update_one({"_id": tid}, {"$set": {
                "protection_missing": False,
                "protection_state": "RESOLVED",
                "confirmed_stop_loss": float(tr["stop_loss"]),
                "protection_resolved_at": now_iso}})
            resolved += 1
            continue
        accounts_blocked.add(account_id)
        mod = tr.get("pending_modification")
        if mod:
            # Round 11 item 8 — a queued emergency FULL_CLOSE is not
            # "resolved": monitor for the broker ack, re-queue on timeout,
            # escalate to an operational alert after repeated silence. The
            # account-wide entry halt stays active the whole time.
            if (mod.get("type") == "FULL_CLOSE"
                    and tr.get("protection_state") == "EMERGENCY_CLOSE_PENDING"):
                req = str(mod.get("requested_at") or "")
                try:
                    age = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(req)).total_seconds()
                except ValueError:
                    age = CLOSE_ACK_ESCALATION_SEC + 1
                if age > CLOSE_ACK_ESCALATION_SEC:
                    retries = int(tr.get("emergency_close_attempts") or 0) + 1
                    sets = {"pending_modification": {
                        **mod, "requested_at": now_iso, "retry": retries}}
                    if (retries >= CLOSE_ALERT_ATTEMPTS
                            and not tr.get("emergency_close_alerted")):
                        sets["emergency_close_alerted"] = True
                        logger.critical(
                            "EMERGENCY CLOSE unacknowledged after %d retries "
                            "trade=%s account=%s — operational attention "
                            "required", retries, tid, account_id)
                        if tr.get("user_id"):
                            try:
                                await db.notifications.insert_one({
                                    "user_id": tr["user_id"],
                                    "type": "emergency_close_stuck",
                                    "title": "Emergency close unconfirmed",
                                    "message": (
                                        f"Emergency close of {tr.get('symbol')} "
                                        f"(trade {tid}) has not been confirmed "
                                        f"by the broker after {retries} "
                                        f"attempts. Check the MT5 terminal."),
                                    "created_at": now_iso, "read": False})
                            except Exception:  # noqa: BLE001
                                pass
                    await db.trades.update_one(
                        {"_id": tid},
                        {"$set": sets,
                         "$inc": {"emergency_close_attempts": 1}})
            continue                      # waiting for the EA to ack
        attempts = int(tr.get("protection_repair_attempts") or 0)
        if attempts >= MAX_STOP_ATTEMPTS:
            # protection could not be established → flatten the position
            await db.trades.update_one({"_id": tid}, {"$set": {
                "protection_state": "EMERGENCY_CLOSE_PENDING",
                "close_requested": True,
                "close_reason": "emergency_unprotected",
                "pending_modification": {
                    "type": "FULL_CLOSE",
                    "reason": "protection_unrecoverable",
                    "requested_at": now_iso}}})
            closes_queued += 1
            continue
        acc, lookup_reason = await find_account(db, account_id)
        if acc is None:
            logger.error(
                "protection guard: account %s lookup failed (%s) — equity "
                "unavailable, failing closed for trade %s",
                account_id, lookup_reason, tid)
        equity = float((acc or {}).get("equity") or 0)
        sl = calculate_emergency_stop(
            float(tr.get("entry_price") or 0), tr.get("action"),
            float(tr.get("lot_size") or 0), tr.get("symbol") or "", equity)
        if sl is None:
            await db.trades.update_one({"_id": tid}, {"$set": {
                "protection_state": "EMERGENCY_CLOSE_PENDING",
                "close_requested": True,
                "close_reason": "emergency_unprotected",
                "pending_modification": {
                    "type": "FULL_CLOSE",
                    "reason": "emergency_stop_uncomputable",
                    "requested_at": now_iso}}})
            closes_queued += 1
            continue
        await db.trades.update_one({"_id": tid}, {
            "$set": {"protection_state": "EMERGENCY_STOP_PENDING",
                     "requested_stop_loss": sl,
                     "pending_modification": {
                         "type": "MODIFY_SL", "new_sl": sl,
                         "reason": "emergency_protection",
                         "requested_at": now_iso}},
            "$inc": {"protection_repair_attempts": 1}})
        stops_queued += 1
        if tr.get("user_id"):
            await db.notifications.insert_one({
                "user_id": tr["user_id"], "type": "protection_alert",
                "title": "Unprotected position — emergency stop queued",
                "message": (f"{tr.get('symbol')} ticket {tr.get('mt5_ticket')} "
                            f"had no stop-loss; emergency SL {sl} requested "
                            f"(attempt {attempts + 1}/{MAX_STOP_ATTEMPTS})."),
                "created_at": now_iso, "read": False})
    # gate scalp entries + count conservative risk while unresolved
    from scalp.engine import account_risk_state, set_protection_block
    open_flagged = await db.trades.distinct(
        "account_id", {"status": "open", "protection_missing": True})
    open_flagged = {str(a) for a in open_flagged}
    for account_id in accounts_blocked | open_flagged:
        blocked = account_id in open_flagged
        set_protection_block(account_id, blocked)
        ars = account_risk_state(account_id)
        key = "unprotected_positions"
        if blocked:
            # Round 10 item 2 (review) — unknown exposure is an UNCONDITIONAL
            # VETO (set_protection_block above), not an arbitrary dollar
            # amount. Record a capped, clearly-labelled estimate for
            # monitoring only: 0.5% of equity, never a fixed $50 floor that
            # distorts small accounts.
            try:
                acc, _lr = await find_account(db, account_id)
                equity = float((acc or {}).get("equity") or 0)
            except Exception:  # noqa: BLE001
                equity = 0.0
            ars.unknown_risk = True
            ars.add_stop_risk(
                key, round(equity * CONSERVATIVE_RISK_PCT / 100.0, 2))
        else:
            ars.unknown_risk = False
            ars.remove_stop_risk(key)
    if resolved or stops_queued or closes_queued:
        logger.warning("protection guard: resolved=%d stops_queued=%d "
                       "closes_queued=%d", resolved, stops_queued, closes_queued)
    return {"resolved": resolved, "stops_queued": stops_queued,
            "closes_queued": closes_queued,
            "accounts_blocked": sorted(open_flagged)}
