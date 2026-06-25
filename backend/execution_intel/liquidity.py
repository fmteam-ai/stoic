"""Liquidity scoring — 0-100 quality score for executing a trade *right now*.

Deterministic, fast. Inputs we actually have (no real DOM from MT5 yet):
  • spread vs rolling median  (best signal of liquidity quality)
  • tick velocity              (ticks per minute from quote history)
  • session                    (london/ny windows score higher than tokyo/off-hours)

Composite weights chosen to be intuitive:
  spread_score  : 50% — bad spread dominates everything
  tick_score    : 30% — frequent updates = liquid book
  session_score : 20% — soft contextual prior

Returns:
  {
    "symbol": str,
    "score": int 0..100,
    "tier": "EXCELLENT"|"GOOD"|"FAIR"|"POOR"|"AVOID",
    "components": {spread_score, tick_score, session_score},
    "details": {spread_observed, spread_median, ratio, ticks_per_min, session},
    "reason": str,
  }
"""
from datetime import datetime, timezone, timedelta
import logging

from microstructure import current_session

logger = logging.getLogger("execution.liquidity")


def _tier(score: int) -> str:
    if score >= 80:
        return "EXCELLENT"
    if score >= 60:
        return "GOOD"
    if score >= 40:
        return "FAIR"
    if score >= 20:
        return "POOR"
    return "AVOID"


def _spread_score(observed: float | None, median: float | None) -> tuple[int, float | None]:
    """100 when at-or-below median; 0 when ≥4× median. Smooth in between."""
    if observed is None or median is None or median <= 0:
        return 60, None  # neutral when unknown
    ratio = observed / median
    if ratio <= 1.0:
        return 100, ratio
    if ratio >= 4.0:
        return 0, ratio
    # Linear: ratio=1→100, ratio=4→0
    return int(round(100 - (ratio - 1.0) * (100 / 3.0))), ratio


def _tick_score(ticks_per_min: float) -> int:
    """60/min = excellent (tick-per-second), 5/min = poor, 1/min = avoid."""
    if ticks_per_min >= 60:
        return 100
    if ticks_per_min <= 1:
        return 0
    # Logarithmic feels natural here but linear is fine for a heuristic
    return int(round((ticks_per_min - 1) / (60 - 1) * 100))


def _session_score(session: dict) -> tuple[int, str]:
    """High-vol overlap windows score best. We treat london+ny overlap (12-16 UTC)
    as the sweet spot."""
    primary = (session or {}).get("primary") or "any"
    if (session or {}).get("is_high_volume_window"):
        return 100, primary
    if primary in ("london", "ny"):
        return 75, primary
    if primary == "tokyo":
        return 50, primary
    return 30, primary  # off-hours / weekend


async def _ticks_per_min(db, symbol: str, lookback_min: int = 5) -> float:
    """Estimate tick velocity from `db.quotes_history` if we maintain one,
    else fall back to counting recent rows in `db.market_ticks` if present.
    Returns 0 when no history available (treated as POOR by tick_score)."""
    since = (datetime.now(timezone.utc) - timedelta(minutes=lookback_min)).isoformat()
    try:
        # We log position_ticks in db on each EA heartbeat (iter-28). Use them
        # as the cheapest live-tick estimator we have.
        coll = db.position_ticks if hasattr(db, "position_ticks") else None
        if coll is None:
            return 0.0
        n = await db.position_ticks.count_documents({
            "symbol": symbol, "ts": {"$gte": since},
        })
    except Exception:  # noqa: BLE001
        n = 0
    return n / max(lookback_min, 1)


async def score(
    *,
    db,
    symbol: str,
    account: dict | None = None,
) -> dict:
    sym = symbol.upper()
    spreads = (account or {}).get("current_spreads") or {}
    medians = (account or {}).get("median_spreads") or {}
    observed = spreads.get(sym)
    median = medians.get(sym)
    spread_pts, ratio = _spread_score(observed, median)

    tpm = await _ticks_per_min(db, sym)
    tick_pts = _tick_score(tpm)

    session = current_session()
    sess_pts, primary = _session_score(session)

    composite = int(round(0.5 * spread_pts + 0.3 * tick_pts + 0.2 * sess_pts))
    composite = max(0, min(100, composite))
    tier = _tier(composite)

    # Build a one-line reason for the UI
    parts = []
    if ratio is not None:
        parts.append(f"spread {ratio:.1f}× median")
    parts.append(f"{tpm:.1f} ticks/min")
    parts.append(f"{primary} session")

    return {
        "symbol": sym,
        "score": composite,
        "tier": tier,
        "components": {"spread_score": spread_pts,
                       "tick_score": tick_pts,
                       "session_score": sess_pts},
        "details": {
            "spread_observed": observed,
            "spread_median":   median,
            "spread_ratio":    ratio,
            "ticks_per_min":   round(tpm, 2),
            "session":         primary,
        },
        "reason": " · ".join(parts),
    }
