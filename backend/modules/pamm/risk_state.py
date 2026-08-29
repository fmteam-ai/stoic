"""O(1) maintained risk state (iter-149, hardened iter-156).

The PAMM guard's trade-derived telemetry (open exposure, weekly/daily
realized loss, slippage samples, loss streak) previously required full
document scans on EVERY authorization. This module maintains a per-account
snapshot in `pamm_risk_state`, guarded by a CONSISTENCY BASIS.

P0-B (iter-156): the basis is a CONTENT fingerprint, not just counts —
index-covered aggregations over the mutable risk inputs:

    open trades:  count + Σlot_size + Σrisk_pct
    week closed:  count + Σpnl

So an open position changing SIZE or RISK, a partial fill, a manual broker
position change surfaced by Position Truth, or a corrected closed-trade
P&L each change the fingerprint and invalidate the maintained state
IMMEDIATELY — even when document counts stay constant. A hard age bound
(RISK_STATE_MAX_AGE_S, default 600s) additionally covers in-place updates
outside the fingerprint (e.g. slippage stamps on old fills).

NAV drawdown peaks get the same treatment in `pamm_nav_peak`, keyed by the
append-only snapshot count per program.

FAIL-SAFE: every helper degrades to None/misses on ANY error — the guard
then falls back to the full recompute path. The cache can slow things
down when broken, never mislead.
"""
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


async def compute_basis(db, acct_id: str, week0: str, day0: str) -> dict:
    """Content fingerprint of every mutable risk input (P0-B, iter-156):
    two index-covered aggregations — a size/risk change on an OPEN
    position or a P&L correction on a CLOSED trade invalidates the state
    immediately, even with unchanged document counts."""
    open_g = {"n": 0, "lots": 0.0, "risk": 0.0}
    async for g in db.trades.aggregate([
            {"$match": {"account_id": acct_id,
                        "status": {"$in": ["open", "pending"]}}},
            {"$group": {"_id": None, "n": {"$sum": 1},
                        "lots": {"$sum": _num("lot_size")},
                        "risk": {"$sum": _num("risk_pct")}}}]):
        open_g = g
    week_g = {"n": 0, "pnl": 0.0}
    async for g in db.trades.aggregate([
            {"$match": {"account_id": acct_id, "status": "closed",
                        "closed_at": {"$gte": week0}}},
            {"$group": {"_id": None, "n": {"$sum": 1},
                        "pnl": {"$sum": _num("pnl")}}}]):
        week_g = g
    return {"open_count": int(open_g.get("n") or 0),
            "open_lots": round(float(open_g.get("lots") or 0.0), 6),
            "open_risk": round(float(open_g.get("risk") or 0.0), 6),
            "week_closed": int(week_g.get("n") or 0),
            "week_pnl": round(float(week_g.get("pnl") or 0.0), 6),
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
