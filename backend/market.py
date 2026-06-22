"""Market data — multi-source, free, no-key.

Routing per asset class to dodge rate limits:
  - Crypto      -> CoinGecko (live + history)
  - Gold (XAU)  -> gold-api.com live + CoinGecko PAXG for history proxy
  - Forex       -> open.er-api.com live + Frankfurter (ECB) for history

All sources are keyless, generous-rate, and don't IP-throttle our cluster.
"""
import time
import asyncio
import httpx
from datetime import datetime, timezone, timedelta

# ---------- Symbol map ----------
SYMBOL_MAP = {
    # Commodities
    "XAUUSD": {"asset": "commodity"},
    # Forex (base/quote)
    "EURUSD": {"asset": "forex", "base": "EUR", "quote": "USD"},
    "GBPUSD": {"asset": "forex", "base": "GBP", "quote": "USD"},
    "USDJPY": {"asset": "forex", "base": "USD", "quote": "JPY"},
    "AUDUSD": {"asset": "forex", "base": "AUD", "quote": "USD"},
    "USDCAD": {"asset": "forex", "base": "USD", "quote": "CAD"},
    "USDCHF": {"asset": "forex", "base": "USD", "quote": "CHF"},
    "NZDUSD": {"asset": "forex", "base": "NZD", "quote": "USD"},
    # Crypto (CoinGecko id)
    "BTCUSD": {"asset": "crypto", "cg_id": "bitcoin"},
    "ETHUSD": {"asset": "crypto", "cg_id": "ethereum"},
    "SOLUSD": {"asset": "crypto", "cg_id": "solana"},
    "BNBUSD": {"asset": "crypto", "cg_id": "binancecoin"},
    "XRPUSD": {"asset": "crypto", "cg_id": "ripple"},
    "ADAUSD": {"asset": "crypto", "cg_id": "cardano"},
    "DOGEUSD": {"asset": "crypto", "cg_id": "dogecoin"},
}

UA = {"User-Agent": "Mozilla/5.0 (compatible; EmergentTradingBot/1.0)"}

_cache: dict = {}
_locks: dict = {}


def _key(symbol: str) -> str:
    return symbol.upper().replace("/", "").replace("-", "")


def _cache_get(k):
    item = _cache.get(k)
    if item is None:
        return None
    exp, val = item
    if time.time() > exp:
        _cache.pop(k, None)
        return None
    return val


def _cache_set(k, v, ttl):
    _cache[k] = (time.time() + ttl, v)


def _lock_for(k) -> asyncio.Lock:
    if k not in _locks:
        _locks[k] = asyncio.Lock()
    return _locks[k]


def asset_type_of(symbol: str) -> str:
    sym = _key(symbol)
    return SYMBOL_MAP.get(sym, {}).get("asset", "stock")


def supported_symbols() -> list:
    return list(SYMBOL_MAP.keys())


# ---------- Crypto via CoinGecko ----------
async def _cg_quote(cg_id: str) -> dict:
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": cg_id, "vs_currencies": "usd", "include_24hr_change": "true"}
    async with httpx.AsyncClient(timeout=15.0, headers=UA, follow_redirects=True) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        if cg_id not in data:
            raise RuntimeError(f"CoinGecko: {cg_id} not found")
        d = data[cg_id]
        price = float(d["usd"])
        chg_pct = float(d.get("usd_24h_change") or 0)
        return {
            "price": price, "bid": price, "ask": price,
            "change": round(price * chg_pct / 100, 5),
            "change_pct": round(chg_pct, 4),
            "high": None, "low": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


async def _cg_history(cg_id: str, days: int = 365) -> list:
    """1-year daily history from CoinGecko."""
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart"
    params = {"vs_currency": "usd", "days": str(days), "interval": "daily"}
    async with httpx.AsyncClient(timeout=20.0, headers=UA, follow_redirects=True) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        prices = data.get("prices", [])
        history = []
        for ts_ms, price in prices:
            date_str = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            history.append({
                "date": date_str,
                "open": float(price), "high": float(price),
                "low": float(price), "close": float(price), "volume": 0,
            })
        return history


# ---------- Gold via gold-api.com + PAXG ----------
async def _gold_quote() -> dict:
    url = "https://api.gold-api.com/price/XAU"
    async with httpx.AsyncClient(timeout=15.0, headers=UA, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        d = r.json()
        price = float(d["price"])
        return {
            "price": price, "bid": price, "ask": price,
            "change": 0.0, "change_pct": 0.0,
            "high": None, "low": None,
            "timestamp": d.get("updatedAt") or datetime.now(timezone.utc).isoformat(),
        }


async def _gold_history() -> list:
    """Use PAX Gold (PAXG) as a 1:1 proxy for XAU/USD historical."""
    return await _cg_history("pax-gold")


# ---------- Forex via open.er-api.com + Frankfurter ----------
async def _fx_quote(base: str, quote: str) -> dict:
    url = f"https://open.er-api.com/v6/latest/{base}"
    async with httpx.AsyncClient(timeout=15.0, headers=UA, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        d = r.json()
        if d.get("result") != "success":
            raise RuntimeError("er-api: " + str(d.get("error-type", "unknown")))
        rate = float(d["rates"][quote])
        return {
            "price": rate, "bid": rate, "ask": rate,
            "change": 0.0, "change_pct": 0.0,
            "high": None, "low": None,
            "timestamp": d.get("time_last_update_utc") or datetime.now(timezone.utc).isoformat(),
        }


async def _fx_history(base: str, quote: str) -> list:
    """Frankfurter ECB rates — supports common FX pairs."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=400)
    url = f"https://api.frankfurter.dev/v1/{start.isoformat()}..{end.isoformat()}"
    params = {"base": base, "symbols": quote}
    async with httpx.AsyncClient(timeout=20.0, headers=UA, follow_redirects=True) as c:
        r = await c.get(url, params=params)
        r.raise_for_status()
        d = r.json()
        rates = d.get("rates", {})
        history = []
        for date_str in sorted(rates.keys()):
            price = float(rates[date_str].get(quote, 0))
            if price <= 0:
                continue
            history.append({
                "date": date_str,
                "open": price, "high": price, "low": price, "close": price, "volume": 0,
            })
        return history[-365:]


# ---------- Public API ----------
async def get_quote(symbol: str) -> dict:
    sym = _key(symbol)
    cache_key = f"quote:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return {**cached, "cached": True}

    async with _lock_for(cache_key):
        cached = _cache_get(cache_key)
        if cached:
            return {**cached, "cached": True}

        meta = SYMBOL_MAP.get(sym)
        if not meta:
            raise RuntimeError(f"Symbol {sym} not supported")

        try:
            if meta["asset"] == "crypto":
                q = await _cg_quote(meta["cg_id"])
            elif meta["asset"] == "commodity":
                q = await _gold_quote()
            elif meta["asset"] == "forex":
                q = await _fx_quote(meta["base"], meta["quote"])
            else:
                raise RuntimeError("Unknown asset class")
            result = {"symbol": sym, **q}
            _cache_set(cache_key, result, ttl_seconds_for_quote(meta["asset"]))
            return {**result, "cached": False}
        except httpx.HTTPError as e:
            raise RuntimeError(f"Quote fetch failed for {sym}: {e}")


def ttl_seconds_for_quote(asset: str) -> int:
    # Crypto moves fast — 60s. Gold spot moves slow — 120s. FX rates daily — 600s.
    if asset == "crypto":
        return 60
    if asset == "commodity":
        return 120
    return 600


async def get_history(symbol: str) -> list:
    sym = _key(symbol)
    cache_key = f"history:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    async with _lock_for(cache_key):
        cached = _cache_get(cache_key)
        if cached:
            return cached

        meta = SYMBOL_MAP.get(sym)
        if not meta:
            raise RuntimeError(f"Symbol {sym} not supported")

        try:
            if meta["asset"] == "crypto":
                hist = await _cg_history(meta["cg_id"])
            elif meta["asset"] == "commodity":
                hist = await _gold_history()
            elif meta["asset"] == "forex":
                hist = await _fx_history(meta["base"], meta["quote"])
            else:
                hist = []
            _cache_set(cache_key, hist, 21600)  # 6h
            return hist
        except httpx.HTTPError as e:
            raise RuntimeError(f"History fetch failed for {sym}: {e}")


def _ttl_seconds_for_quote(symbol: str):  # backward compat alias
    return ttl_seconds_for_quote(asset_type_of(symbol))


# ---------- Indicators (re-exported from indicators module) ----------
from indicators import compute_indicators  # noqa: E402,F401
