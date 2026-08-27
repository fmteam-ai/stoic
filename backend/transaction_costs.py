"""Dynamic Transaction Cost Engine (v59 #6) — replaces the static
cost_r=0.05 assumption. Expected cost = spread + commission + expected
slippage + latency cost + swap, conditioned on the user's own realized
evidence per symbol. The uncertainty engine then requires the LOWER
bound of edge to clear expected cost + a safety margin."""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("transaction.costs")

SYMBOL_DEFAULT_SPREAD_R = {"XAUUSD": 0.03, "BTCUSD": 0.04, "US30": 0.03,
                           "NAS100": 0.03, "EURUSD": 0.02}
COMMISSION_R = 0.01
SAFETY_MARGIN_R = 0.02
SLIPPAGE_R_DEFAULT = 0.02
COST_FLOOR_R = 0.03


def spread_r_of(signal: dict | None, symbol: str) -> tuple[float, str]:
    """Spread in R units from the live signal when possible."""
    sig = signal or {}
    spread = sig.get("spread") or (sig.get("execution") or {}).get("spread")
    try:
        entry = float(sig.get("entry_price") or 0)
        sl = float(sig.get("stop_loss") or 0)
        risk = abs(entry - sl)
        if spread is not None and risk > 0:
            return (round(max(0.0, min(0.3, float(spread) / risk)), 3),
                    "live_signal")
    except (TypeError, ValueError):
        pass
    base = str(symbol or "").upper()[:6]
    return SYMBOL_DEFAULT_SPREAD_R.get(base, 0.03), "symbol_default"


async def _median_slippage_r(db, user_id: str, symbol: str):
    import re
    since = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    vals = []
    async for o in db.trade_outcomes.find(
            {"user_id": user_id,
             "symbol": {"$regex": f"^{re.escape(symbol[:6])}",
                        "$options": "i"},
             "closed_at": {"$gte": since}},
            {"signals.slippage_ratio": 1}).sort(
            "closed_at", -1).limit(150):
        v = (o.get("signals") or {}).get("slippage_ratio")
        if v is not None:
            vals.append(abs(float(v)))
    if not vals:
        return None, 0
    vals.sort()
    return round(vals[len(vals) // 2], 3), len(vals)


async def _latency_r(db, user_id: str, symbol: str):
    import re
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    totals = []
    async for t in db.trades.find(
            {"user_id": user_id,
             "symbol": {"$regex": f"^{re.escape(symbol[:6])}",
                        "$options": "i"},
             "status": "closed", "closed_at": {"$gte": since}},
            {"latency_trace": 1}).limit(150):
        lt = t.get("latency_trace") or {}
        if lt.get("t7_ms") and lt.get("t9_ms"):
            totals.append(int(lt["t9_ms"]) - int(lt["t7_ms"]))
    if not totals:
        return 0.0, 0
    p50 = sorted(totals)[len(totals) // 2]
    return round(min(0.05, max(0.0, (p50 - 300) / 20000)), 3), len(totals)


async def expected_cost_r(db, user_id: str, symbol: str,
                          signal: dict | None = None,
                          scope: str | None = None) -> dict:
    spread_r, spread_basis = spread_r_of(signal, symbol)
    slip_r, slip_n = await _median_slippage_r(db, user_id, symbol)
    lat_r, lat_n = await _latency_r(db, user_id, symbol)
    swap_r = 0.02 if "swing" in str(scope or "").lower() else 0.0
    components = {"spread_r": spread_r,
                  "commission_r": COMMISSION_R,
                  "slippage_r": slip_r if slip_r is not None
                  else SLIPPAGE_R_DEFAULT,
                  "latency_r": lat_r, "swap_r": swap_r}
    total = round(max(COST_FLOOR_R, sum(components.values())), 3)
    return {"cost_r": total,
            "required_edge_r": round(total + SAFETY_MARGIN_R, 3),
            "safety_margin_r": SAFETY_MARGIN_R,
            "components": components,
            "basis": {"spread": spread_basis,
                      "slippage_samples": slip_n,
                      "latency_samples": lat_n,
                      "slippage_source": "realized" if slip_r is not None
                      else "default"},
            "symbol": str(symbol or "").upper()}
