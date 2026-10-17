"""#53 — real entry costs per trade, captured from the EA so the break-even buffer is not a fixed default.

  • spread_at_entry_pips  — the symbol spread the EA reported in its latest heartbeat when the fill was confirmed;
  • commission_entry / commission_per_lot — the IN deal's commission (MT5 reports it negative) from /bridge/external-deal;
  • commission_pips       — round-trip commission (entry + exit ≈ 2 × entry) expressed in pips of this trade's symbol,
                            which is what trade_manager.scalp_cost_pips adds to the spread for the BE stop.
"""
from __future__ import annotations

from pip_utils import base_symbol, pip_value_usd_per_lot


def normalize_spread_keys(spreads: dict | None) -> dict:
    """v1.60.64 #86 — heartbeat spreads keyed by the BROKER name (EA 1.67: GOLD#, XAUUSD+, XAUUSD.crp) also get their
    BASE key (XAUUSD), so every reader that looks up `current_spreads[<base>]` (spread filter, EV cost, adaptive sizing)
    finds them. Raw keys are kept; an existing base key (the broker really calls it XAUUSD) is never overwritten."""
    out: dict = {}
    for k, v in (spreads or {}).items():
        out[str(k).upper()] = v
    for k, v in list(out.items()):
        b = base_symbol(k)
        if b and b not in out:
            out[b] = v
    return out


def spreads_by_base(spreads: dict | None) -> dict:
    """{base: spread} — one value per base symbol (exact base key first, else the first broker-named key)."""
    out: dict = {}
    norm = normalize_spread_keys(spreads)
    for k, v in norm.items():
        b = base_symbol(k)
        if b and (b not in out or k == b):
            out[b] = v
    return out


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


SPREAD_STALE_SEC = 600


def spread_coverage(accounts: list, cfgs: list, now=None) -> list:
    """v1.60.64 #86 — Bot Health: per live account, the TRADED base symbols whose spread the EA does not report
    (or reported > 10 min ago) while that symbol's market is open. → [{label, ea_version, missing: [base], stale}]
    Before EA 1.67 a broker-renamed gold symbol (GOLD#, XAUUSD+, XAUUSD.crp) never reported a spread."""
    from datetime import datetime, timezone
    from microstructure import is_market_closed
    now = now or datetime.now(timezone.utc)
    by_acc = {str(c.get("account_id")): c for c in cfgs or [] if c.get("account_id")}
    default = next((c for c in cfgs or [] if not c.get("account_id")), {})
    out = []
    for a in accounts or []:
        cfg = by_acc.get(str(a.get("_id"))) or default or {}
        traded = sorted({base_symbol(s) for s in (cfg.get("symbols") or []) if s})
        traded = [b for b in traded if b and not is_market_closed(b, now)]
        if not traded:
            continue
        have = spreads_by_base(a.get("current_spreads"))
        stale = False
        ts = a.get("spreads_updated_at")
        try:
            stale = ts is None or (now - datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds() > SPREAD_STALE_SEC
        except ValueError:
            stale = True
        missing = [b for b in traded if b not in have]
        if missing or stale:
            out.append({"label": a.get("label") or a.get("broker_server") or "unnamed", "ea_version": a.get("ea_version"),
                        "missing": missing if not stale else traded, "stale": stale})
    return out
