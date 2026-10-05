"""H9 — live quote-currency → USD rates for pip values and cross-pair risk.

`pip_value_usd_per_lot` needs "USD per unit of the QUOTE currency" for every
non-USD-quoted pair (EURGBP → GBP, CADJPY → JPY). The fixed QUOTE_USD_APPROX
table was last right on the day it was typed; this module resolves the rate
from today's prices instead, in order of trust:

  1. the user's broker tick for USD<ccy> (1/price) or <ccy>USD (price)   → "broker_tick"
  2. the public quote for the same pair                                   → "public_quote"
  3. the approximate table, flagged stale and logged once an hour         → "approx_table"

Rates are cached for 60 s per (ccy, user) in-process.
"""
import logging
import time

from pip_utils import QUOTE_USD_APPROX, _is_ccy_pair, base_symbol, pip_value_usd_per_lot

logger = logging.getLogger("fx_rates")

RATE_TTL_S = 60
_CACHE: dict = {}          # (ccy, user_id) -> {"exp": monotonic, "rate": float, "source": str, "at": iso}
_WARNED: dict = {}         # ccy -> monotonic of the last approx-table warning
_LAST: dict = {}           # ccy -> last resolution (for the health view)

# quote currencies whose USD pair is quoted ccy/USD (price = USD per unit)
_CCY_FIRST = {"EUR", "GBP", "AUD", "NZD"}


def _pair_for(ccy: str) -> tuple[str, bool]:
    """(symbol, invert) — invert=True when the pair is USD<ccy> (USD per unit = 1/price)."""
    return (f"{ccy}USD", False) if ccy in _CCY_FIRST else (f"USD{ccy}", True)


async def _live_price(symbol: str, user_id: str | None) -> tuple[float, str]:
    try:
        from intraday_features import broker_live_price
        px = await broker_live_price(symbol, user_id)
        if px and px > 0:
            return float(px), "broker_tick"
    except Exception as e:  # noqa: BLE001
        logger.debug("broker tick %s unavailable: %s", symbol, type(e).__name__)
    try:
        from market import get_quote
        q = await get_quote(symbol)
        px = float((q or {}).get("price") or 0)
        if px > 0:
            return px, "public_quote"
    except Exception as e:  # noqa: BLE001
        logger.debug("public quote %s unavailable: %s", symbol, type(e).__name__)
    return 0.0, "none"


async def quote_usd(ccy: str, user_id: str | None = None) -> tuple[float | None, str]:
    """USD per one unit of `ccy`. (1.0, "usd") for USD; (None, "unknown") for unknown currencies."""
    ccy = (ccy or "").upper()
    if ccy == "USD":
        return 1.0, "usd"
    key = (ccy, user_id)
    now = time.monotonic()
    ent = _CACHE.get(key)
    if ent and ent["exp"] > now:
        return ent["rate"], ent["source"]
    symbol, invert = _pair_for(ccy)
    px, source = await _live_price(symbol, user_id)
    if px > 0:
        rate = (1.0 / px) if invert else px
    else:
        rate = QUOTE_USD_APPROX.get(ccy)
        source = "approx_table" if rate is not None else "unknown"
        if rate is not None and now - _WARNED.get(ccy, -1e9) > 3600:
            _WARNED[ccy] = now
            logger.warning("H9: no live %s rate — pip values for %s-quoted pairs use the approximate table (%.4f)",
                           ccy, ccy, rate)
    from datetime import datetime, timezone
    _CACHE[key] = {"exp": now + RATE_TTL_S, "rate": rate, "source": source}
    _LAST[ccy] = {"rate": rate, "source": source, "symbol": symbol, "at": datetime.now(timezone.utc).isoformat(),
                  "stale": source in ("approx_table", "unknown")}
    return rate, source


async def quote_usd_for_symbol(symbol: str | None, user_id: str | None = None) -> tuple[float | None, str]:
    """Rate for the symbol's QUOTE currency; (None, "n/a") when the pair is USD-quoted / USD-based / not FX
    (pip_value_usd_per_lot handles those from the price alone)."""
    base = base_symbol(symbol)
    if not _is_ccy_pair(base):
        return None, "n/a"
    quote = base[3:]
    if quote == "USD" or base.startswith("USD"):
        return None, "n/a"
    return await quote_usd(quote, user_id)


async def pip_value_usd_per_lot_live(symbol: str | None, account_type: str | None = None,
                                     price: float | None = None, user_id: str | None = None) -> tuple[float, str]:
    """pip_value_usd_per_lot with today's quote-currency rate. Returns (value, rate_source)."""
    rate, source = await quote_usd_for_symbol(symbol, user_id)
    return pip_value_usd_per_lot(symbol, account_type, price=price, quote_usd=rate), source


def rates_snapshot() -> dict:
    """Health view: every quote currency resolved in this process and where its rate came from."""
    stale = sorted(c for c, r in _LAST.items() if r.get("stale"))
    return {"ttl_s": RATE_TTL_S, "rates": dict(sorted(_LAST.items())), "stale_currencies": stale,
            "approx_table": QUOTE_USD_APPROX}


def reset_cache() -> None:
    _CACHE.clear()
    _LAST.clear()
    _WARNED.clear()
