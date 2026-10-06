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


# MT5-standard contract sizes — used to derive USD-risk-at-entry per trade.
# (Mirrors the table in Trades.jsx so SL distance × lot × contract = $ risk.)
_CONTRACT_SIZE = {
    "XAUUSD": 100,     # 1 lot = 100 oz
    "XAGUSD": 5000,    # 1 lot = 5000 oz
    "BTCUSD": 1,
    "ETHUSD": 1,
}


def _r_multiple(row: dict):
    """Return realized R-multiple for a closed trade, or None if SL is missing.

    R = pnl / risk_at_entry,  where  risk_at_entry = |entry - SL| × lot × contract.
    Capped at ±10R to keep outliers from skewing the average.
    """
    try:
        entry = float(row.get("entry_price") or 0)
        sl = float(row.get("stop_loss") or 0)
        lot = float(row.get("lot_size") or 0)
        pnl = float(row.get("pnl") or 0)
        if entry <= 0 or sl <= 0 or lot <= 0:
            return None
        cs = _CONTRACT_SIZE.get((row.get("symbol") or "").upper(), 1)
        risk = abs(entry - sl) * lot * cs
        if risk <= 0:
            return None
        r = pnl / risk
        # Clamp pathological outliers
        return max(-10.0, min(10.0, r))
    except Exception:
        return None


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
    cursor = db.trades.find(
        {"user_id": user_id, "status": "closed"},
        {"pnl": 1, "symbol": 1, "action": 1, "mode": 1, "origin": 1,
         "signal_id": 1, "opened_at": 1, "closed_at": 1, "partial_closed": 1,
         "breakeven_set": 1, "trail_active": 1, "close_reason": 1})
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

    # P1-05 — attribution figures are PLATFORM BOOKS (closed trades), labelled so on
    # every chart; broker-reconciled figures live on Verified Performance only.
    from chart_provenance import build as _provenance
    return {
        "provenance": _provenance(provider="stoic_trades", source_kind="derived",
                                  points=[{"date": r.get("opened_at")} for r in rows],
                                  note="platform books from closed trades — NOT broker-reconciled; "
                                       "attested figures: Verified Performance"),
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



# UTC hour ranges for the dedicated session-breakdown card.
# Matches _session_label() — kept here so the UI can render the time window
# under each bucket without a second round-trip.
SESSION_WINDOWS = [
    ("ASIA",    "Tokyo · 00:00–07:00 UTC",       set(range(0, 7))),
    ("LONDON",  "London · 07:00–13:00 UTC",      set(range(7, 13))),
    ("OVERLAP", "London-NY · 13:00–16:00 UTC",   set(range(13, 16))),
    ("NY",      "New York · 16:00–21:00 UTC",    set(range(16, 21))),
    ("OFF",     "Off-hours · 21:00–24:00 UTC",   set(range(21, 24))),
]


def _session_key_4buckets(dt: Optional[datetime]) -> Optional[str]:
    """Split the London-NY overlap as its own bucket (the user explicitly asked
    for Asia / London / NY / **Overlap**). Differs from _session_label which
    folds the overlap into 'NEW YORK' for the existing legacy slice.
    """
    if dt is None:
        return None
    h = dt.hour
    for key, _label, hours in SESSION_WINDOWS:
        if h in hours:
            return key
    return None


async def compute_sessions(user_id: str) -> dict:
    """Per-session performance summary — Asia / London / Overlap / NY / Off-hours.

    Returns 5 buckets always (even if count=0) so the UI can render placeholders
    instead of hiding sessions the bot hasn't traded yet. Each bucket carries:
        count, wins, losses, win_rate, avg_pnl, total_pnl,
        avg_r, expectancy_r, best_r, worst_r,
        r_sample_count   (number of trades with valid SL → R could be computed)
    Plus the headline `best_session_by_r` and `best_session_by_pnl`.
    """
    db = get_db()
    cursor = db.trades.find(
        {"user_id": user_id, "status": "closed"},
        {"opened_at": 1, "pnl": 1, "symbol": 1, "entry_price": 1,
         "stop_loss": 1, "lot_size": 1})
    closed = await cursor.to_list(length=5000)

    enriched = []
    for t in closed:
        opened = _parse_iso(t.get("opened_at"))
        bucket = _session_key_4buckets(opened)
        if bucket is None:
            continue
        row = {
            "pnl": float(t.get("pnl") or 0),
            "symbol": t.get("symbol"),
            "entry_price": t.get("entry_price"),
            "stop_loss": t.get("stop_loss"),
            "lot_size": t.get("lot_size"),
        }
        row["r_multiple"] = _r_multiple(row)
        row["bucket"] = bucket
        enriched.append(row)

    by_bucket: Dict[str, list] = defaultdict(list)
    for r in enriched:
        by_bucket[r["bucket"]].append(r)

    out_buckets = []
    for key, label, _hours in SESSION_WINDOWS:
        items = by_bucket.get(key, [])
        n = len(items)
        if n == 0:
            out_buckets.append({
                "key": key, "label": label, "count": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "avg_pnl": 0.0, "total_pnl": 0.0,
                "avg_r": None, "expectancy_r": None,
                "best_r": None, "worst_r": None, "r_sample_count": 0,
            })
            continue
        pnls = [it["pnl"] for it in items]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        rs = [it["r_multiple"] for it in items if it["r_multiple"] is not None]
        avg_r = round(sum(rs) / len(rs), 2) if rs else None
        # Expectancy-R = (win_rate × avg_win_R) + (loss_rate × avg_loss_R)
        wins_r = [r for r in rs if r > 0]
        losses_r = [r for r in rs if r < 0]
        expectancy_r = None
        if rs:
            wr = len(wins_r) / len(rs)
            avg_win_r = (sum(wins_r) / len(wins_r)) if wins_r else 0
            avg_loss_r = (sum(losses_r) / len(losses_r)) if losses_r else 0
            expectancy_r = round(wr * avg_win_r + (1 - wr) * avg_loss_r, 2)
        out_buckets.append({
            "key": key, "label": label,
            "count": n, "wins": wins, "losses": losses,
            "win_rate": round(100 * wins / n, 1),
            "avg_pnl": round(sum(pnls) / n, 2),
            "total_pnl": round(sum(pnls), 2),
            "avg_r": avg_r,
            "expectancy_r": expectancy_r,
            "best_r": round(max(rs), 2) if rs else None,
            "worst_r": round(min(rs), 2) if rs else None,
            "r_sample_count": len(rs),
        })

    # Overall (rolled-up) summary across all sessions for the headline tiles
    all_rows = enriched
    overall = {
        "count": len(all_rows),
        "win_rate": round(100 * sum(1 for r in all_rows if r["pnl"] > 0) / len(all_rows), 1) if all_rows else 0,
        "total_pnl": round(sum(r["pnl"] for r in all_rows), 2),
    }
    all_rs = [r["r_multiple"] for r in all_rows if r["r_multiple"] is not None]
    overall["avg_r"] = round(sum(all_rs) / len(all_rs), 2) if all_rs else None
    overall["r_sample_count"] = len(all_rs)

    # Headline ranks — only consider buckets with ≥3 sampled trades to avoid
    # ranking by one lucky outlier. Falls back to None when nobody qualifies.
    def _rank(metric_key):
        eligible = [b for b in out_buckets if b["count"] >= 3 and b.get(metric_key) is not None]
        if not eligible:
            return None
        return max(eligible, key=lambda b: b[metric_key])["key"]

    return {
        "overall": overall,
        "buckets": out_buckets,
        "best_session_by_r": _rank("avg_r"),
        "best_session_by_pnl": _rank("total_pnl"),
        "min_sample_for_ranking": 3,
    }
