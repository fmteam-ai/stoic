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
# N-R5 — a live rate more than this far from the approximate table is a bad tick
# (USDJPY 15000 instead of 150 would size a CADJPY lot ~100× too large, and Safety
# Guardian would agree because it uses the same rate). Rejected → table + ops alert.
SANITY_BAND = 0.25
_CACHE: dict = {}          # (ccy, user_id) -> {"exp": monotonic, "rate": float, "source": str, "at": iso}
_WARNED: dict = {}         # ccy -> monotonic of the last approx-table warning
_LAST: dict = {}           # ccy -> last resolution (for the health view)
_REJECTED: dict = {}       # ccy -> {"rate", "source", "at"} of the last rejected live rate


def rate_is_sane(ccy: str, rate: float) -> bool:
    """True when `rate` (USD per unit of ccy) is within ±SANITY_BAND of the approximate table.
    Unknown currencies (no table entry) cannot be checked and are accepted."""
    ref = QUOTE_USD_APPROX.get(ccy)
    if not ref or not rate or rate <= 0:
        return bool(rate and rate > 0)
    return abs(rate / ref - 1.0) <= SANITY_BAND

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
    rate = None
    if px > 0:
        live = (1.0 / px) if invert else px
        if rate_is_sane(ccy, live):
            rate = live
        else:
            # N-R5 — reject the live rate, fall back to the table, make it visible
            from datetime import datetime as _dt, timezone as _tz
            _REJECTED[ccy] = {"rate": live, "source": source, "symbol": symbol, "price": px,
                              "at": _dt.now(_tz.utc).isoformat()}
            logger.error("H9/N-R5: %s rate %.6f from %s (%s=%.4f) is outside ±%d%% of the table (%.4f) — "
                         "REJECTED, using the approximate table", ccy, live, source, symbol, px,
                         int(SANITY_BAND * 100), QUOTE_USD_APPROX.get(ccy) or 0.0)
            await _alert_rejected(ccy, live, source, symbol, px)
            source = "rejected_live"
    if rate is None:
        rate = QUOTE_USD_APPROX.get(ccy)
        if source != "rejected_live":
            source = "approx_table" if rate is not None else "unknown"
        if rate is not None and source == "approx_table" and now - _WARNED.get(ccy, -1e9) > 3600:
            _WARNED[ccy] = now
            logger.warning("H9: no live %s rate — pip values for %s-quoted pairs use the approximate table (%.4f)",
                           ccy, ccy, rate)
    from datetime import datetime, timezone
    _CACHE[key] = {"exp": now + RATE_TTL_S, "rate": rate, "source": source}
    _LAST[ccy] = {"rate": rate, "source": source, "symbol": symbol, "at": datetime.now(timezone.utc).isoformat(),
                  "stale": source in ("approx_table", "unknown", "rejected_live"),
                  "rejected": _REJECTED.get(ccy)}
    return rate, source


async def _alert_rejected(ccy: str, live: float, source: str, symbol: str, px: float) -> None:
    try:
        from database import get_db
        from alerting import raise_alert
        db = get_db()
        if db is None:
            return
        await raise_alert(db, "fx_rate_rejected", "warning",
                          f"live {ccy} rate {live:.6f} ({source}, {symbol}={px:.4f}) is outside ±{int(SANITY_BAND * 100)}% "
                          f"of the reference {QUOTE_USD_APPROX.get(ccy):.4f} — sizing and Safety Guardian fell back to the "
                          f"approximate table for {ccy}-quoted pairs; check the broker feed",
                          dedup_key=f"fx_rate_rejected:{ccy}",
                          meta={"ccy": ccy, "rate": live, "source": source, "symbol": symbol, "price": px})
    except Exception as e:  # noqa: BLE001 — never block sizing on alerting
        logger.debug("fx rejected alert failed: %s", type(e).__name__)


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
    return {"ttl_s": RATE_TTL_S, "sanity_band": SANITY_BAND, "rates": dict(sorted(_LAST.items())),
            "stale_currencies": stale, "rejected": dict(sorted(_REJECTED.items())), "approx_table": QUOTE_USD_APPROX}


def reset_cache() -> None:
    _CACHE.clear()
    _LAST.clear()
    _WARNED.clear()
    _REJECTED.clear()
