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
import os
from datetime import datetime, timezone

from pip_utils import pip_value_usd_per_lot_strict  # noqa: F401 (re-export)

logger = logging.getLogger(__name__)

MAX_STOP_ATTEMPTS = 3          # MODIFY_SL retries before FULL_CLOSE
EMERGENCY_RISK_PCT = 0.5       # emergency stop sized to 0.5% of equity
CONSERVATIVE_RISK_PCT = 0.5    # unknown position counted at 0.5% equity
FALLBACK_PRICE_PCT = 0.5       # stop distance cap: 0.5% of entry price
CLOSE_ACK_ESCALATION_SEC = 120  # re-queue FULL_CLOSE if no EA ack by then
CLOSE_ALERT_ATTEMPTS = 3        # operational alert after this many retries
REPAIR_TIME_BUDGET_SEC = 8      # per-sweep processing budget (round 12)
REPAIR_QUEUE_ALERT = 100        # global escalation threshold (round 12)
# Round 14 item 9 — broker symbol specs older than this are not trusted for
# stop-constraint math (EA-side clamps remain the broker enforcement).
SYMBOL_SPEC_MAX_AGE_SEC = int(os.environ.get(
    "SYMBOL_SPEC_MAX_AGE_SEC", "86400"))


def looks_like_object_id(value: str) -> bool:
    """Dependency-free ObjectId-shape check (24 hex chars) — classification
    must not depend on whether BSON happens to be installed."""
    return (isinstance(value, str) and len(value) == 24
            and all(c in "0123456789abcdefABCDEF" for c in value))


def rounding_digits(step: float) -> int:
    """Round 13 item 4 — decimal places implied by a price step (tick or
    pip size), instead of a hardcoded 5-decimal FX assumption."""
    from decimal import Decimal, InvalidOperation
    try:
        exp = Decimal(str(step)).normalize().as_tuple().exponent
    except (InvalidOperation, ValueError):
        return 5
    return max(0, -int(exp)) if isinstance(exp, int) else 5


def broker_stop_constraints(account: dict | None, symbol: str) -> dict | None:
    """Round 13 item 5 — precise broker stop-placement constraints
    (SYMBOL_TRADE_STOPS_LEVEL / FREEZE_LEVEL in points) tracked per symbol
    from EA heartbeats, converted to price distances. None when the broker
    never reported them — the EA stays the broker-side enforcement then."""
    from pip_utils import base_symbol
    specs = (account or {}).get("symbol_specs") or {}
    spec = specs.get(base_symbol(symbol)) or specs.get(str(symbol).upper())
    if not isinstance(spec, dict):
        return None
    # round 14 item 9 — stale specs are not trusted: fall back to
    # constraint-free math (the EA clamps broker-side) and log for refresh.
    updated = (account or {}).get("symbol_specs_updated_at")
    if updated:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(str(updated))).total_seconds()
        except ValueError:
            age = None
        if age is not None and age > SYMBOL_SPEC_MAX_AGE_SEC:
            logger.warning(
                "symbol specs for %s stale (%ds > %ds) — ignoring broker "
                "stop constraints until the EA refreshes them",
                symbol, int(age), SYMBOL_SPEC_MAX_AGE_SEC)
            return None
    point = float(spec.get("point") or 0)
    if point <= 0:
        return None
    return {"min_stop_distance_px":
            float(spec.get("stops_level_points") or 0) * point,
            "freeze_distance_px":
            float(spec.get("freeze_level_points") or 0) * point}


async def find_account(db, account_id: str, oid_parser="auto") -> tuple:
    """Round 11/12 item 7 — centralized account lookup with an EXPLICIT
    reason: 'ok', 'invalid_id', 'missing', 'db_error'. Tries an ObjectId
    lookup first (when the id is ObjectId-shaped and a parser is available),
    then a raw string _id, so a valid account stored under another
    identifier format is never mistaken for a missing one.

    Round 14 — the ObjectId parser is an INJECTED dependency (oid_parser):
    'auto' imports bson when installed, None forces string-only lookup, or
    pass any callable. Behavior no longer depends on which packages happen
    to be importable in the test environment."""
    if not account_id:
        return None, "invalid_id"
    shaped = looks_like_object_id(account_id)
    parser = oid_parser
    if parser == "auto":
        try:
            from bson import ObjectId as parser
        except ImportError:
            parser = None
    try:
        acc = None
        if shaped and parser is not None:
            acc = await db.accounts.find_one({"_id": parser(account_id)})
        if acc is None:
            acc = await db.accounts.find_one({"_id": account_id})
        if acc is None:
            return None, ("missing" if shaped else "invalid_id")
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
                             symbol: str, equity: float,
                             broker_constraints: dict | None = None
                             ) -> float | None:
    """Budget-based protective stop: distance = risk budget / (lot × pip
    value), capped at FALLBACK_PRICE_PCT of the entry price. Round 9 item 5:
    the budget is NEVER silently inflated — if equity is too small to place
    a meaningful stop, return None and let the guard escalate to an
    emergency full close instead."""
    if not entry or not lot:
        return None
    from pip_utils import pip_size, pip_value_usd_per_lot_strict
    from scalp.instruments import approved
    # round 12 item 5 — NEVER fabricate a pip value: unpriceable symbol →
    # None → the caller queues an emergency close instead.
    pv = pip_value_usd_per_lot_strict(symbol)
    if pv is None:
        return None
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
    # round 13 item 5 — a budget distance TIGHTER than the broker's minimum
    # stop distance / freeze level can never be placed; the budget is NEVER
    # silently widened — escalate to an emergency close instead.
    if broker_constraints:
        min_dist_px = max(
            float(broker_constraints.get("min_stop_distance_px") or 0),
            float(broker_constraints.get("freeze_distance_px") or 0))
        if min_dist_px > 0 and dist_px < min_dist_px:
            return None
    sl = entry - dist_px if (direction or "BUY").upper() == "BUY" else entry + dist_px
    # round 12 item 6 / round 13 item 4 — snap to the instrument's tick size
    # and round to the digits IMPLIED by that tick; unknown tick falls back
    # to pip-derived digits + 1 (never a hardcoded 5-decimal FX assumption).
    cfg = approved(symbol)
    tick = float(getattr(cfg, "tick_size", 0) or 0) if cfg else 0.0
    if tick > 0:
        return round(round(sl / tick) * tick, rounding_digits(tick))
    return round(sl, rounding_digits(pip) + 1)


async def repair_unprotected_positions(db) -> dict:
    """One sweep of the recovery state machine. Idempotent; safe to run
    every reconcile interval."""
    now_iso = datetime.now(timezone.utc).isoformat()
    q = {"status": "open", "protection_missing": True}
    # Round 12 item 4 — no fixed 50-trade cap: priority-ordered cursor
    # (largest exposure proxy first, then oldest) processed inside a time
    # budget; queue metrics + global escalation when the backlog grows.
    total_awaiting = await db.trades.count_documents(q)
    oldest_doc = await db.trades.find_one(q, {"opened_at": 1},
                                          sort=[("opened_at", 1)])
    oldest_age_sec = None
    if oldest_doc and oldest_doc.get("opened_at"):
        try:
            oldest_age_sec = int(
                (datetime.now(timezone.utc)
                 - datetime.fromisoformat(oldest_doc["opened_at"]))
                .total_seconds())
        except ValueError:
            pass
    if total_awaiting > REPAIR_QUEUE_ALERT:
        logger.critical(
            "protection repair backlog %d exceeds %d — global operational "
            "escalation (oldest unresolved %ss)",
            total_awaiting, REPAIR_QUEUE_ALERT, oldest_age_sec)
    import time as _time
    deadline = _time.monotonic() + REPAIR_TIME_BUDGET_SEC
    resolved = stops_queued = closes_queued = processed = 0
    accounts_blocked: set = set()
    async for tr in db.trades.find(q).sort([("lot_size", -1),
                                            ("opened_at", 1)]):
        if _time.monotonic() > deadline:
            logger.warning(
                "protection repair time budget hit after %d/%d trades — "
                "remainder next sweep", processed, total_awaiting)
            break
        processed += 1
        tid = tr["_id"]
        account_id = str(tr.get("account_id") or "")
        # Round 12 item 7 — protection resolves ONLY on broker-confirmed
        # evidence (confirmed_stop_loss from an EA modification ack or a
        # broker position snapshot). A locally proposed/stale stop_loss
        # value is NOT sufficient.
        confirmed = float(tr.get("confirmed_stop_loss") or 0)
        if confirmed != 0.0:
            await db.trades.update_one({"_id": tid}, {"$set": {
                "protection_missing": False,
                "protection_state": "RESOLVED",
                "confirmed_stop_loss": confirmed,
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
            float(tr.get("lot_size") or 0), tr.get("symbol") or "", equity,
            broker_constraints=broker_stop_constraints(
                acc, tr.get("symbol") or ""))
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
            "accounts_blocked": sorted(open_flagged),
            "awaiting": total_awaiting, "processed": processed,
            "oldest_unresolved_age_sec": oldest_age_sec}
