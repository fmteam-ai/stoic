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


def _risk_per_lot_usd(signal: dict | None, symbol: str):
    """USD stop-risk of 1.0 lot from the live signal's entry/SL."""
    try:
        from portfolio_risk import position_risk_usd
        r = position_risk_usd({"symbol": symbol, "lot": 1.0,
                               "entry_price": (signal or {}).get(
                                   "entry_price"),
                               "stop_loss": (signal or {}).get(
                                   "stop_loss")})
        return r if r > 0 else None
    except Exception:  # noqa: BLE001
        return None


async def _configured_commission_r(db, user_id: str, account_id,
                                   risk_per_lot):
    """Broker/account-specific: bot_configs.commission_usd_per_lot_side."""
    if not account_id or not risk_per_lot:
        return None
    cfg = await db.bot_configs.find_one(
        {"user_id": user_id, "account_id": str(account_id)},
        {"commission_usd_per_lot_side": 1})
    per_side = float((cfg or {}).get("commission_usd_per_lot_side") or 0)
    if per_side <= 0:
        return None
    return round(min(0.2, per_side * 2.0 / risk_per_lot), 4)


async def _realized_deal_cost_r(db, account_id, symbol: str, field: str,
                                risk_per_lot, user_id: str | None = None):
    """Median realized |commission|/|swap| per lot from broker deals.
    Scoped to the caller's own trade evidence (defence-in-depth on top of
    the route-level account ownership check)."""
    import re
    if not account_id or not risk_per_lot:
        return None, 0
    q = {"account_id": str(account_id),
         "symbol": {"$regex": f"^{re.escape(symbol[:6])}",
                    "$options": "i"},
         field: {"$nin": [None, 0]}}
    if user_id:
        q["user_id"] = user_id
    vals = []
    async for d in db.broker_deals.find(
            q, {field: 1, "lots": 1}).sort("deal_time", -1).limit(200):
        lots = float(d.get("lots") or 0)
        if lots > 0:
            vals.append(abs(float(d[field])) / lots)
    if not vals:
        return None, 0
    vals.sort()
    per_lot = vals[len(vals) // 2]
    return round(min(0.2, per_lot / risk_per_lot), 4), len(vals)


async def expected_cost_r(db, user_id: str, symbol: str,
                          signal: dict | None = None,
                          scope: str | None = None,
                          account_id: str | None = None) -> dict:
    account_id = account_id or (signal or {}).get("account_id")
    spread_r, spread_basis = spread_r_of(signal, symbol)
    slip_r, slip_n = await _median_slippage_r(db, user_id, symbol)
    lat_r, lat_n = await _latency_r(db, user_id, symbol)
    risk_per_lot = _risk_per_lot_usd(signal, symbol)
    # commission: account config → realized broker deals → default
    commission_r, commission_source = None, "default"
    try:
        commission_r = await _configured_commission_r(
            db, user_id, account_id, risk_per_lot)
        if commission_r is not None:
            commission_source = "account_config"
    except Exception:  # noqa: BLE001
        pass
    comm_n = 0
    if commission_r is None:
        try:
            commission_r, comm_n = await _realized_deal_cost_r(
                db, account_id, symbol, "commission", risk_per_lot,
                user_id=user_id)
            if commission_r is not None:
                commission_source = "realized_deals"
        except Exception:  # noqa: BLE001
            pass
    if commission_r is None:
        commission_r = COMMISSION_R
    # swap: realized broker deals → scope heuristic
    swap_r, swap_source, swap_n = None, "heuristic", 0
    is_swing = "swing" in str(scope or "").lower()
    try:
        swap_r, swap_n = await _realized_deal_cost_r(
            db, account_id, symbol, "swap", risk_per_lot,
            user_id=user_id)
        if swap_r is not None:
            swap_source = "realized_deals"
            if not is_swing:
                swap_r = round(swap_r * 0.2, 4)   # intraday rarely rolls
    except Exception:  # noqa: BLE001
        pass
    if swap_r is None:
        swap_r = 0.02 if is_swing else 0.0
    components = {"spread_r": spread_r,
                  "commission_r": commission_r,
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
                      else "default",
                      "commission_source": commission_source,
                      "commission_samples": comm_n,
                      "swap_source": swap_source,
                      "swap_samples": swap_n,
                      "account_id": str(account_id) if account_id
                      else None},
            "symbol": str(symbol or "").upper()}
