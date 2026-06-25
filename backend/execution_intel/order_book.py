"""Order-book pulse — light-weight depth/velocity proxy without real DOM.

Full L2 DOM requires `MarketBookGet()` in the EA (planned for v1.28+).
For now we expose the same shape (`pulse(symbol)`) but compute it from
data we actually have: spread ratio + tick velocity + recent volatility.

Output:
  {
    "symbol": str,
    "tick_velocity_per_min": float,
    "spread_ratio": float|None,
    "recent_atr_pct": float|None,
    "book_pulse": "ACTIVE"|"NORMAL"|"THIN"|"FROZEN",
    "depth_score": int,        # 0..100 — proxy for "would my market order eat depth?"
    "notes": [str, ...],
  }
"""
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger("execution.order-book")


def _classify(velocity: float, spread_ratio: float | None) -> str:
    if velocity >= 30 and (spread_ratio is None or spread_ratio <= 1.5):
        return "ACTIVE"
    if velocity >= 5 and (spread_ratio is None or spread_ratio <= 2.5):
        return "NORMAL"
    if velocity >= 1:
        return "THIN"
    return "FROZEN"


def _depth_score(velocity: float, spread_ratio: float | None) -> int:
    """Tighter spread + higher velocity = higher implied depth.
    Pure heuristic — proxy for real DOM until EA v1.28 ships."""
    v_norm = min(1.0, velocity / 60.0)  # cap at 1 tick/sec
    if spread_ratio is None or spread_ratio <= 0:
        s_norm = 0.5
    else:
        s_norm = max(0.0, min(1.0, (4.0 - spread_ratio) / 3.0))
    return int(round((0.6 * s_norm + 0.4 * v_norm) * 100))


async def pulse(db, *, symbol: str, account: dict | None = None,
                 lookback_min: int = 5) -> dict:
    sym = symbol.upper()
    since = (datetime.now(timezone.utc) - timedelta(minutes=lookback_min)).isoformat()
    notes: list[str] = []

    # Tick velocity from the position_ticks WS broadcast log (iter-28 EA).
    n = 0
    try:
        n = await db.position_ticks.count_documents({
            "symbol": sym, "ts": {"$gte": since},
        })
    except Exception as e:  # noqa: BLE001
        notes.append(f"position_ticks unavailable: {e}")
    velocity = n / max(lookback_min, 1)

    spread_ratio = None
    if account:
        observed = (account.get("current_spreads") or {}).get(sym)
        median = (account.get("median_spreads") or {}).get(sym)
        if observed and median and median > 0:
            spread_ratio = round(observed / median, 3)

    book_pulse = _classify(velocity, spread_ratio)
    depth = _depth_score(velocity, spread_ratio)

    if velocity < 1:
        notes.append("No recent ticks — confirm EA is heartbeating.")
    if spread_ratio is None:
        notes.append("No median-spread baseline on this account — order-book proxy is using session-only signals.")

    return {
        "symbol": sym,
        "tick_velocity_per_min": round(velocity, 2),
        "spread_ratio": spread_ratio,
        "recent_atr_pct": None,
        "book_pulse": book_pulse,
        "depth_score": depth,
        "notes": notes,
        "real_dom_available": False,   # set True when EA v1.28+ MarketBookGet relay ships
    }
