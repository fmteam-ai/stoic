"""Performance Attribution Analytics — slices closed trades by every meaningful
dimension and surfaces which combinations are actually profitable.

Output is purely derived from the `trades` and `signals` collections — no extra
storage needed. Endpoint is intentionally heavy (full table-scan over closed
trades) but trade counts are small enough (<10k typical) that this is fine.
"""
from datetime import datetime, timezone
from collections import defaultdict
from typing import List, Dict, Optional
from bson import ObjectId

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


def _hour_bucket(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return f"{dt.hour:02d}:00"


def _dow_label(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.weekday()]


def _session_label(dt: Optional[datetime]) -> Optional[str]:
    """UTC-based trading session label."""
    if dt is None:
        return None
    h = dt.hour
    if 0 <= h < 7:
        return "ASIAN"
    if 7 <= h < 13:
        return "LONDON"
    if 13 <= h < 16:
        return "LONDON-NY OVERLAP"
    if 16 <= h < 21:
        return "NEW YORK"
    return "OFF-HOURS"


def _confidence_bucket(conf) -> Optional[str]:
    if conf is None:
        return None
    try:
        c = float(conf)
    except Exception:
        return None
    if c < 50:
        return "<50"
    if c < 60:
        return "50-60"
    if c < 70:
        return "60-70"
    if c < 80:
        return "70-80"
    if c < 90:
        return "80-90"
    return "90+"


def _summarise(rows: List[dict]) -> dict:
    n = len(rows)
    if n == 0:
        return {"count": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0, "best": 0, "worst": 0}
    pnls = [float(r.get("pnl") or 0) for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    total = sum(pnls)
    return {
        "count": n,
        "win_rate": round(100 * wins / n, 1),
        "total_pnl": round(total, 2),
        "avg_pnl": round(total / n, 2),
        "best": round(max(pnls), 2),
        "worst": round(min(pnls), 2),
    }


def _bucketize(rows: List[dict], key_fn) -> List[dict]:
    groups: Dict[str, list] = defaultdict(list)
    for r in rows:
        k = key_fn(r)
        if k is None:
            continue
        groups[k].append(r)
    out = []
    for k, items in groups.items():
        s = _summarise(items)
        s["key"] = k
        out.append(s)
    out.sort(key=lambda x: x["total_pnl"], reverse=True)
    return out


async def compute_attribution(user_id: str) -> dict:
    """Compute the full attribution payload for one user.

    Pulls every closed trade, joins with its signal (for confidence/regime),
    annotates with session/hour/dow buckets, then aggregates many ways.
    """
    db = get_db()
    cursor = db.trades.find({"user_id": user_id, "status": "closed"})
    closed = await cursor.to_list(length=5000)

    # Join signals to fetch confidence + regime at the time of trade
    signal_ids = [t.get("signal_id") for t in closed if t.get("signal_id")]
    signal_oids = []
    for sid in signal_ids:
        try:
            signal_oids.append(ObjectId(sid))
        except Exception:
            continue
    sig_map = {}
    if signal_oids:
        sig_cursor = db.signals.find({"_id": {"$in": signal_oids}})
        async for s in sig_cursor:
            sig_map[str(s["_id"])] = s

    # Enrich every row
    rows = []
    for t in closed:
        sig = sig_map.get(str(t.get("signal_id") or ""), {})
        opened_dt = _parse_iso(t.get("opened_at"))
        closed_dt = _parse_iso(t.get("closed_at"))
        rows.append({
            "pnl": float(t.get("pnl") or 0),
            "symbol": t.get("symbol"),
            "action": t.get("action"),
            "mode": t.get("mode") or "live",
            "origin": t.get("origin") or "auto",
            "confidence": sig.get("confidence"),
            "risk_level": sig.get("risk_level"),
            "regime": (sig.get("regime") or {}).get("regime") if isinstance(sig.get("regime"), dict) else sig.get("regime"),
            "opened_at": opened_dt,
            "closed_at": closed_dt,
            "partial_closed": bool(t.get("partial_closed")),
            "breakeven_set": bool(t.get("breakeven_set")),
            "trail_active": bool(t.get("trail_active")),
            "close_reason": t.get("close_reason"),
        })

    overall = _summarise(rows)

    return {
        "overall": overall,
        "by_symbol": _bucketize(rows, lambda r: r["symbol"]),
        "by_action": _bucketize(rows, lambda r: r["action"]),
        "by_mode": _bucketize(rows, lambda r: r["mode"]),
        "by_origin": _bucketize(rows, lambda r: r["origin"]),
        "by_risk_level": _bucketize(rows, lambda r: r["risk_level"]),
        "by_session": _bucketize(rows, lambda r: _session_label(r["opened_at"])),
        "by_hour": _bucketize(rows, lambda r: _hour_bucket(r["opened_at"])),
        "by_day_of_week": _bucketize(rows, lambda r: _dow_label(r["opened_at"])),
        "by_confidence": _bucketize(rows, lambda r: _confidence_bucket(r["confidence"])),
        "by_regime": _bucketize(rows, lambda r: r["regime"]),
        "by_close_reason": _bucketize(rows, lambda r: r["close_reason"]),
        "by_protection": _bucketize(rows, lambda r: (
            "Partial-Close + BE + Trail" if r["partial_closed"] and r["breakeven_set"] and r["trail_active"]
            else "Partial-Close + BE" if r["partial_closed"] and r["breakeven_set"]
            else "Break-Even only" if r["breakeven_set"]
            else "No protection fired"
        )),
        # Cross-slice: which symbol × session combinations win most
        "by_symbol_session": _bucketize(rows, lambda r: (
            f"{r['symbol']} · {_session_label(r['opened_at'])}"
            if r["symbol"] and r["opened_at"] else None
        )),
        # Symbol × confidence bucket
        "by_symbol_confidence": _bucketize(rows, lambda r: (
            f"{r['symbol']} · conf {_confidence_bucket(r['confidence'])}"
            if r["symbol"] and r["confidence"] is not None else None
        )),
    }
