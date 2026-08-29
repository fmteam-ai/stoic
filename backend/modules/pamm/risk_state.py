"""O(1) maintained risk state (iter-149, hardened iter-156).

The PAMM guard's trade-derived telemetry (open exposure, weekly/daily
realized loss, slippage samples, loss streak) previously required full
document scans on EVERY authorization. This module maintains a per-account
snapshot in `pamm_risk_state`, guarded by a CONSISTENCY BASIS.

P0 (iter-157): the basis is a COMPOSITION-sensitive content fingerprint —
sum-based aggregates were blind to a position changing symbol/side/SL while
totals stayed equal (BUY XAUUSD 1.0/1% → SELL EURUSD 1.0/1% kept the sums
identical). Open positions are FEW and bounded, so the open-side identity
is now a sha256 over the sorted per-position tuples of every
risk-affecting field:

    open trades:  sha256[(id, symbol, action, side, lot_size, risk_pct,
                          stop_loss, confirmed_stop_loss, status), ...]
    week closed:  count + Σpnl
    day  closed:  count + Σpnl

Every event the reviewer enumerated — order accepted, fill, partial fill,
open, close, size/side/symbol/SL change, manual position, broker
reconciliation — mutates an open-trade document (or the open set) and so
changes the fingerprint IMMEDIATELY; P&L corrections and closed_at moves
change the week/day sums. This is an authoritative identity computed from
Position Truth itself: no writer hooks to forget. A hard age bound
(RISK_STATE_MAX_AGE_S, default 600s) additionally covers in-place updates
outside the fingerprint (e.g. slippage stamps on old fills).

NAV drawdown peaks get the same treatment in `pamm_nav_peak`, keyed by the
append-only snapshot count per program.

FAIL-SAFE: every helper degrades to None/misses on ANY error — the guard
then falls back to the full recompute path. The cache can slow things
down when broken, never mislead.
"""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("pamm.risk_state")

_indexes_ready = False


def _max_age_s() -> int:
    try:
        return int(os.environ.get("RISK_STATE_MAX_AGE_S", "600"))
    except ValueError:
        return 600


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_s(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        return None


async def ensure_indexes(db) -> None:
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        await db.trades.create_index([("account_id", 1), ("status", 1)])
        await db.trades.create_index(
            [("account_id", 1), ("status", 1), ("closed_at", -1)])
        await db.pamm_nav_snapshots.create_index([("program_id", 1)])
        await db.pamm_risk_state.create_index("account_id", unique=True)
        await db.pamm_nav_peak.create_index("program_id", unique=True)
        _indexes_ready = True
    except Exception as e:  # noqa: BLE001 — cache infra must never crash
        logger.warning("risk_state index setup failed: %s", e)


def _num(field: str) -> dict:
    return {"$convert": {"input": f"${field}", "to": "double",
                         "onError": 0.0, "onNull": 0.0}}


# every field that changes what the position IS, risk-wise
RISK_COMPOSITION_FIELDS = ("symbol", "action", "side", "lot_size",
                           "risk_pct", "stop_loss", "confirmed_stop_loss",
                           "status")


def _norm(v):
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    return str(v)


async def _open_composition_fp(db, acct_id: str) -> tuple[int, str]:
    """Order-independent sha256 identity of the OPEN portfolio composition
    (iter-157). Open positions are bounded and few, so this is a tight
    projected read — not the O(N) history scan the cache exists to avoid."""
    rows = []
    async for tr in db.trades.find(
            {"account_id": acct_id, "status": {"$in": ["open", "pending"]}},
            {f: 1 for f in RISK_COMPOSITION_FIELDS}).limit(1000):
        rows.append([str(tr.get("_id"))]
                    + [_norm(tr.get(f)) for f in RISK_COMPOSITION_FIELDS])
    rows.sort(key=lambda r: r[0])
    fp = hashlib.sha256(
        json.dumps(rows, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return len(rows), fp


async def compute_basis(db, acct_id: str, week0: str, day0: str) -> dict:
    """Composition-sensitive consistency identity (P0 iter-157): open-side
    sha256 fingerprint + week/day closed count/Σpnl. Symbol, side, size,
    risk% or SL changing on an open position — even with identical totals —
    changes the identity and invalidates the maintained state immediately."""
    open_count, open_fp = await _open_composition_fp(db, acct_id)
    week_g = {"n": 0, "pnl": 0.0}
    async for g in db.trades.aggregate([
            {"$match": {"account_id": acct_id, "status": "closed",
                        "closed_at": {"$gte": week0}}},
            {"$group": {"_id": None, "n": {"$sum": 1},
                        "pnl": {"$sum": _num("pnl")}}}]):
        week_g = g
    day_g = {"n": 0, "pnl": 0.0}
    async for g in db.trades.aggregate([
            {"$match": {"account_id": acct_id, "status": "closed",
                        "closed_at": {"$gte": day0}}},
            {"$group": {"_id": None, "n": {"$sum": 1},
                        "pnl": {"$sum": _num("pnl")}}}]):
        day_g = g
    return {"open_count": open_count, "open_fp": open_fp,
            "week_closed": int(week_g.get("n") or 0),
            "week_pnl": round(float(week_g.get("pnl") or 0.0), 6),
            "day_closed": int(day_g.get("n") or 0),
            "day_pnl": round(float(day_g.get("pnl") or 0.0), 6),
            "week0": week0, "day0": day0}


async def read_state(db, acct_id: str, basis: dict) -> dict | None:
    """Return the maintained payload iff the basis matches and it is
    within the hard age bound — otherwise None (caller recomputes)."""
    try:
        doc = await db.pamm_risk_state.find_one({"account_id": acct_id})
        if not doc:
            return None
        if doc.get("basis") != basis:
            return None
        age = _age_s(doc.get("computed_at"))
        if age is None or age > _max_age_s():
            return None
        return doc.get("payload") or None
    except Exception as e:  # noqa: BLE001
        logger.warning("risk_state read failed acct=%s: %s", acct_id, e)
        return None


async def store_state(db, acct_id: str, basis: dict, payload: dict) -> None:
    try:
        await db.pamm_risk_state.update_one(
            {"account_id": acct_id},
            {"$set": {"basis": basis, "payload": payload,
                      "computed_at": _now_iso()}},
            upsert=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("risk_state store failed acct=%s: %s", acct_id, e)


async def nav_peak(db, program_id: str) -> float | None:
    """All-time NAV peak for a program, O(1) via `pamm_nav_peak` keyed by
    the append-only snapshot count; full rescan only when count moved."""
    try:
        snap_count = await db.pamm_nav_snapshots.count_documents(
            {"program_id": program_id})
        if snap_count == 0:
            return None
        cached = await db.pamm_nav_peak.find_one({"program_id": program_id})
        if cached and cached.get("snap_count") == snap_count:
            return cached.get("peak")
        peak = None
        async for n in db.pamm_nav_snapshots.find(
                {"program_id": program_id},
                {"_id": 0, "nav": 1}).limit(20000):
            peak = n["nav"] if peak is None else max(peak, n["nav"])
        if peak is not None:
            await db.pamm_nav_peak.update_one(
                {"program_id": program_id},
                {"$set": {"peak": peak, "snap_count": snap_count,
                          "computed_at": _now_iso()}},
                upsert=True)
        return peak
    except Exception as e:  # noqa: BLE001
        logger.warning("nav_peak failed pid=%s: %s", program_id, e)
        return None
