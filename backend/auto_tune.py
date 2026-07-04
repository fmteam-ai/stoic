"""Auto-tune confidence threshold from closed-trade analytics.

Builds a per-user, per-symbol minimum-confidence threshold by analysing
the empirical win-rate AND profitability of historical closed trades grouped
into 5-pt buckets. The "auto-tuned" threshold is the lowest bucket where:
  - sample count >= MIN_SAMPLES, AND
  - win_rate >= TARGET_WIN_RATE, AND
  - the bucket is NET PROFITABLE with positive expectancy (iter-42: win rate
    and profit are tied — a bucket that wins often but loses money never
    qualifies).

If no bucket qualifies, fall back to the user's risk-profile minimum.

This is **advisory**: bot_runner consumes it via `effective_min_confidence`
and gates auto-execution. The signal is still generated and persisted for
inspection — only the auto-execute decision is affected.

Results are cached for 15 minutes per (user_id, symbol) to keep the
inner bot loop fast.
"""
import time
from typing import Optional
from collections import defaultdict
from bson import ObjectId

from database import get_db
from risk import get_profile
from objective import expectancy_stats

MIN_SAMPLES = 5            # need at least this many trades in a bucket to trust it
TARGET_WIN_RATE = 55.0     # we want >= 55% win rate
CACHE_TTL_SEC = 900        # 15 min
BUCKETS = [50, 55, 60, 65, 70, 75, 80, 85, 90]  # lower bound of each 5-pt bucket

_cache: dict = {}   # { (user_id, symbol): (expires_at, payload) }


def _bucket_for(conf) -> Optional[int]:
    if conf is None:
        return None
    try:
        c = float(conf)
    except Exception:
        return None
    if c < BUCKETS[0]:
        return None
    for b in reversed(BUCKETS):
        if c >= b:
            return b
    return None


async def _load_rows(user_id: str, symbol: Optional[str]) -> list:
    db = get_db()
    q = {"user_id": user_id, "status": "closed"}
    if symbol:
        q["symbol"] = symbol.upper()
    cursor = db.trades.find(q).sort("closed_at", -1).limit(200)
    trades = await cursor.to_list(length=200)
    if not trades:
        return []

    signal_ids = [t.get("signal_id") for t in trades if t.get("signal_id")]
    oids = []
    for sid in signal_ids:
        try:
            oids.append(ObjectId(sid))
        except Exception:
            continue
    sig_map = {}
    if oids:
        async for s in db.signals.find({"_id": {"$in": oids}}):
            sig_map[str(s["_id"])] = s

    rows = []
    for t in trades:
        sig = sig_map.get(str(t.get("signal_id") or "")) or {}
        rows.append({
            "pnl": float(t.get("pnl") or 0),
            "symbol": t.get("symbol"),
            "confidence": sig.get("confidence"),
        })
    return rows


def _compute_threshold(rows: list, profile_min: float) -> dict:
    buckets = defaultdict(list)
    for r in rows:
        b = _bucket_for(r.get("confidence"))
        if b is None:
            continue
        buckets[b].append(r["pnl"])

    breakdown = []
    for b in BUCKETS:
        pnls = buckets.get(b, [])
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        wr = round(100 * wins / n, 1) if n else 0.0
        stats = expectancy_stats(pnls) if n else {}
        breakdown.append({
            "bucket": b,
            "count": n,
            "win_rate": wr,
            "total_pnl": round(sum(pnls), 2),
            "expectancy_r": stats.get("expectancy_r"),
            "payoff_ratio": stats.get("payoff_ratio"),
        })

    # Lowest qualifying bucket — iter-42: win rate AND profit tied. The
    # bucket must win often enough AND be net profitable with positive
    # expectancy; frequent-but-losing buckets can no longer raise the gate.
    suggested = None
    for entry in breakdown:
        if (entry["count"] >= MIN_SAMPLES
                and entry["win_rate"] >= TARGET_WIN_RATE
                and entry["total_pnl"] > 0
                and (entry["expectancy_r"] or 0) > 0):
            suggested = entry["bucket"]
            break

    total_samples = sum(b["count"] for b in breakdown)

    if suggested is None:
        effective = profile_min
        source = "profile_default"
    else:
        effective = max(float(suggested), float(profile_min))
        source = "auto_tuned"

    return {
        "suggested_threshold": suggested,
        "profile_min": profile_min,
        "effective_threshold": effective,
        "source": source,
        "total_samples": total_samples,
        "breakdown": breakdown,
    }


async def get_auto_threshold(user_id: str, symbol: str, risk_level: str) -> dict:
    """Return the auto-tuned min-confidence threshold for (user, symbol)."""
    sym = (symbol or "").upper()
    cache_key = (user_id, sym)
    now = time.time()
    cached = _cache.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]

    profile = get_profile(risk_level)
    profile_min = float(profile.get("min_confidence", 65))

    rows = await _load_rows(user_id, sym)
    result = _compute_threshold(rows, profile_min)
    result["symbol"] = sym
    result["risk_level"] = risk_level

    _cache[cache_key] = (now + CACHE_TTL_SEC, result)
    return result


async def get_all_thresholds(user_id: str, risk_level: str, symbols: list) -> list:
    """Compute auto-tune thresholds across every active symbol for a user."""
    out = []
    for sym in symbols:
        out.append(await get_auto_threshold(user_id, sym, risk_level))
    return out


def invalidate_cache(user_id: Optional[str] = None) -> None:
    """Drop cache (e.g. after a trade closes so next tick re-evaluates)."""
    if user_id is None:
        _cache.clear()
        return
    for k in [k for k in _cache if k[0] == user_id]:
        _cache.pop(k, None)
