"""#53 — real entry costs per trade, captured from the EA so the break-even buffer is not a fixed default.

  • spread_at_entry_pips  — the symbol spread the EA reported in its latest heartbeat when the fill was confirmed;
  • commission_entry / commission_per_lot — the IN deal's commission (MT5 reports it negative) from /bridge/external-deal;
  • commission_pips       — round-trip commission (entry + exit ≈ 2 × entry) expressed in pips of this trade's symbol,
                            which is what trade_manager.scalp_cost_pips adds to the spread for the BE stop.
"""
from __future__ import annotations

from pip_utils import base_symbol, pip_value_usd_per_lot


def spread_at_entry(account: dict | None, symbol: str | None) -> float | None:
    spreads = (account or {}).get("current_spreads") or {}
    for key in (symbol, base_symbol(symbol)):
        v = spreads.get(key) if key else None
        if v is not None:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                return round(f, 2)
    for k, v in spreads.items():
        if base_symbol(k) == base_symbol(symbol):
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                return round(f, 2)
    return None


def commission_update(symbol: str | None, lots: float, commission: float, *, account_type: str | None = None,
                      price: float | None = None) -> dict:
    """Fields to stamp on the trade from its IN deal. Empty when the deal carries no commission."""
    try:
        lots = float(lots or 0)
        comm = abs(float(commission or 0))
    except (TypeError, ValueError):
        return {}
    if lots <= 0 or comm <= 0:
        return {}
    per_lot = comm / lots
    pip_usd = pip_value_usd_per_lot(symbol, account_type, price=price)
    out = {"commission_entry": round(-comm, 2), "commission_per_lot": round(per_lot, 4)}
    if pip_usd and pip_usd > 0:
        out["commission_pips"] = round(2.0 * per_lot / pip_usd, 3)
    return out
