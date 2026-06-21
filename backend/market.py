"""Alpha Vantage market data integration with in-memory caching.

Alpha Vantage free tier: 25 req/day, 5 req/min — we aggressively cache.
"""
import os
import time
import asyncio
import httpx
from datetime import datetime, timezone
from typing import Optional

ALPHA_BASE = "https://www.alphavantage.co/query"

# In-memory cache: { key: (expires_at_epoch, value) }
_cache: dict = {}
_locks: dict = {}

# Symbol mapping — Alpha Vantage uses different endpoints per asset class.
# We accept the user-facing ticker (XAUUSD, BTCUSD) and translate.
FOREX_PAIRS = {
    "XAUUSD": ("XAU", "USD"),
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "AUDUSD": ("AUD", "USD"),
    "USDCAD": ("USD", "CAD"),
    "USDCHF": ("USD", "CHF"),
    "NZDUSD": ("NZD", "USD"),
}
CRYPTO_PAIRS = {
    "BTCUSD": ("BTC", "USD"),
    "ETHUSD": ("ETH", "USD"),
    "SOLUSD": ("SOL", "USD"),
    "BNBUSD": ("BNB", "USD"),
    "XRPUSD": ("XRP", "USD"),
}


def _key(symbol: str) -> str:
    return symbol.upper().replace("/", "")


def _cache_get(key: str):
    item = _cache.get(key)
    if item is None:
        return None
    expires_at, value = item
    if time.time() > expires_at:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value, ttl_seconds: int):
    _cache[key] = (time.time() + ttl_seconds, value)


def _lock_for(key: str) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def _api_key() -> str:
    return os.environ.get("ALPHA_VANTAGE_KEY", "demo")


async def _http_get(params: dict) -> dict:
    params = {**params, "apikey": _api_key()}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(ALPHA_BASE, params=params)
        r.raise_for_status()
        data = r.json()
        # Alpha Vantage error messages
        if "Error Message" in data:
            raise RuntimeError(data["Error Message"])
        if "Note" in data:  # rate limited
            raise RuntimeError("Rate limit reached. " + data["Note"])
        if "Information" in data and "premium" in data["Information"].lower():
            raise RuntimeError(data["Information"])
        return data


# ---------- Quotes ----------
async def get_quote(symbol: str) -> dict:
    """Return a unified quote dict regardless of asset class."""
    sym = _key(symbol)
    cache_key = f"quote:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return {**cached, "cached": True}

    async with _lock_for(cache_key):
        # Re-check after acquiring lock
        cached = _cache_get(cache_key)
        if cached:
            return {**cached, "cached": True}

        if sym in CRYPTO_PAIRS:
            from_, to_ = CRYPTO_PAIRS[sym]
            data = await _http_get({
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": from_,
                "to_currency": to_,
            })
            rate = data.get("Realtime Currency Exchange Rate", {})
            price = float(rate.get("5. Exchange Rate", 0))
            bid = float(rate.get("8. Bid Price", price) or price)
            ask = float(rate.get("9. Ask Price", price) or price)
            ts = rate.get("6. Last Refreshed", datetime.now(timezone.utc).isoformat())
            result = {
                "symbol": sym, "price": price, "bid": bid, "ask": ask,
                "change": 0.0, "change_pct": 0.0,
                "high": None, "low": None,
                "timestamp": ts,
            }
        elif sym in FOREX_PAIRS:
            from_, to_ = FOREX_PAIRS[sym]
            data = await _http_get({
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": from_,
                "to_currency": to_,
            })
            rate = data.get("Realtime Currency Exchange Rate", {})
            price = float(rate.get("5. Exchange Rate", 0))
            bid = float(rate.get("8. Bid Price", price) or price)
            ask = float(rate.get("9. Ask Price", price) or price)
            ts = rate.get("6. Last Refreshed", datetime.now(timezone.utc).isoformat())
            result = {
                "symbol": sym, "price": price, "bid": bid, "ask": ask,
                "change": 0.0, "change_pct": 0.0,
                "high": None, "low": None,
                "timestamp": ts,
            }
        else:
            # Treat as equity
            data = await _http_get({"function": "GLOBAL_QUOTE", "symbol": sym})
            q = data.get("Global Quote", {})
            price = float(q.get("05. price", 0) or 0)
            change = float(q.get("09. change", 0) or 0)
            change_pct_str = (q.get("10. change percent", "0%") or "0%").replace("%", "")
            try:
                change_pct = float(change_pct_str)
            except ValueError:
                change_pct = 0.0
            result = {
                "symbol": sym, "price": price, "bid": price, "ask": price,
                "change": change, "change_pct": change_pct,
                "high": float(q.get("03. high", 0) or 0),
                "low": float(q.get("04. low", 0) or 0),
                "timestamp": q.get("07. latest trading day", datetime.now(timezone.utc).isoformat()),
            }

        # Cache 60s (free tier rate limit safety)
        _cache_set(cache_key, result, ttl_seconds=60)
        return {**result, "cached": False}


# ---------- Historical (6 months) ----------
async def get_history(symbol: str) -> list:
    """Return list of {date, open, high, low, close} for past ~180 days."""
    sym = _key(symbol)
    cache_key = f"history:{sym}"
    cached = _cache_get(cache_key)
    if cached:
        return cached

    async with _lock_for(cache_key):
        cached = _cache_get(cache_key)
        if cached:
            return cached

        if sym in CRYPTO_PAIRS:
            from_, to_ = CRYPTO_PAIRS[sym]
            data = await _http_get({
                "function": "DIGITAL_CURRENCY_DAILY",
                "symbol": from_,
                "market": to_,
            })
            series = data.get("Time Series (Digital Currency Daily)", {})
            history = []
            for date_str, row in sorted(series.items()):
                history.append({
                    "date": date_str,
                    "open": float(row.get("1. open", 0)),
                    "high": float(row.get("2. high", 0)),
                    "low": float(row.get("3. low", 0)),
                    "close": float(row.get("4. close", 0)),
                    "volume": float(row.get("5. volume", 0)),
                })
            history = history[-180:]
        elif sym in FOREX_PAIRS:
            from_, to_ = FOREX_PAIRS[sym]
            data = await _http_get({
                "function": "FX_DAILY",
                "from_symbol": from_,
                "to_symbol": to_,
                "outputsize": "compact",
            })
            series = data.get("Time Series FX (Daily)", {})
            history = []
            for date_str, row in sorted(series.items()):
                history.append({
                    "date": date_str,
                    "open": float(row.get("1. open", 0)),
                    "high": float(row.get("2. high", 0)),
                    "low": float(row.get("3. low", 0)),
                    "close": float(row.get("4. close", 0)),
                    "volume": 0,
                })
            history = history[-180:]
        else:
            data = await _http_get({
                "function": "TIME_SERIES_DAILY",
                "symbol": sym,
                "outputsize": "compact",
            })
            series = data.get("Time Series (Daily)", {})
            history = []
            for date_str, row in sorted(series.items()):
                history.append({
                    "date": date_str,
                    "open": float(row.get("1. open", 0)),
                    "high": float(row.get("2. high", 0)),
                    "low": float(row.get("3. low", 0)),
                    "close": float(row.get("4. close", 0)),
                    "volume": float(row.get("5. volume", 0)),
                })
            history = history[-180:]

        # Cache 6 hours
        _cache_set(cache_key, history, ttl_seconds=21600)
        return history


# ---------- Indicators (computed locally from history) ----------
def compute_indicators(history: list) -> dict:
    """Compute lightweight TA indicators from daily candles."""
    if not history or len(history) < 30:
        return {}

    closes = [c["close"] for c in history]
    n = len(closes)

    def sma(period):
        if n < period:
            return None
        return sum(closes[-period:]) / period

    def ema(period):
        if n < period:
            return None
        k = 2 / (period + 1)
        e = sum(closes[:period]) / period
        for px in closes[period:]:
            e = px * k + e * (1 - k)
        return e

    def rsi(period=14):
        if n < period + 1:
            return None
        gains, losses = [], []
        for i in range(1, period + 1):
            d = closes[-(period + 1) + i] - closes[-(period + 1) + i - 1]
            (gains if d > 0 else losses).append(abs(d))
        avg_g = sum(gains) / period if gains else 0
        avg_l = sum(losses) / period if losses else 0
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100 - (100 / (1 + rs))

    current = closes[-1]
    sma20 = sma(20)
    sma50 = sma(50)
    sma200 = sma(min(200, n))
    high180 = max(c["high"] for c in history)
    low180 = min(c["low"] for c in history)
    pct_from_high = ((current - high180) / high180) * 100 if high180 else 0
    pct_from_low = ((current - low180) / low180) * 100 if low180 else 0

    # 6-month return
    six_month_return = ((closes[-1] - closes[0]) / closes[0]) * 100 if closes[0] else 0
    # 30-day volatility (stdev of daily returns)
    if n >= 30:
        rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(n - 30, n) if closes[i - 1]]
        mean = sum(rets) / len(rets) if rets else 0
        var = sum((r - mean) ** 2 for r in rets) / len(rets) if rets else 0
        volatility_30d = (var ** 0.5) * 100
    else:
        volatility_30d = 0

    return {
        "current_price": round(current, 5),
        "sma_20": round(sma20, 5) if sma20 else None,
        "sma_50": round(sma50, 5) if sma50 else None,
        "sma_200": round(sma200, 5) if sma200 else None,
        "ema_12": round(ema(12), 5) if ema(12) else None,
        "ema_26": round(ema(26), 5) if ema(26) else None,
        "rsi_14": round(rsi(14), 2) if rsi(14) else None,
        "high_180d": round(high180, 5),
        "low_180d": round(low180, 5),
        "pct_from_high": round(pct_from_high, 2),
        "pct_from_low": round(pct_from_low, 2),
        "six_month_return_pct": round(six_month_return, 2),
        "volatility_30d_pct": round(volatility_30d, 2),
    }


def supported_symbols() -> list:
    return list(FOREX_PAIRS.keys()) + list(CRYPTO_PAIRS.keys())


def asset_type_of(symbol: str) -> str:
    sym = _key(symbol)
    if sym in CRYPTO_PAIRS:
        return "crypto"
    if sym == "XAUUSD":
        return "commodity"
    if sym in FOREX_PAIRS:
        return "forex"
    return "stock"
