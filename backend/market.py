"""Market data — multi-source, free, no-key.

Routing per asset class to dodge rate limits:
  - Crypto      -> CoinGecko (live + history)
  - Gold (XAU)  -> gold-api.com live + CoinGecko PAXG for history proxy
  - Forex       -> open.er-api.com live + Frankfurter (ECB) for history

All sources are keyless, generous-rate, and don't IP-throttle our cluster.
"""
import time
import asyncio
import logging
import httpx
from datetime import datetime, timezone, timedelta

log = logging.getLogger("market")

# ---------- Symbol map ----------
SYMBOL_MAP = {
    # Commodities
    "XAUUSD": {"asset": "commodity"},
    # Equity index CFDs (iter-43) — Yahoo Finance chart API for the
    # underlying cash index; the broker CFD tracks it closely.
    "US30":   {"asset": "index", "yh": "^DJI"},
    "NAS100": {"asset": "index", "yh": "^NDX"},
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


# ---------- Crypto: Coinbase Exchange (primary) → CoinGecko (fallback) ----------
COINBASE_PRODUCT_MAP = {
    "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD", "SOLUSD": "SOL-USD",
    "BNBUSD": "BNB-USD", "XRPUSD": "XRP-USD", "ADAUSD": "ADA-USD",
    "DOGEUSD": "DOGE-USD",
}


async def _coinbase_quote(symbol_key: str) -> dict:
    product = COINBASE_PRODUCT_MAP.get(symbol_key)
    if not product:
        raise RuntimeError(f"Coinbase: {symbol_key} not mapped")
    async with httpx.AsyncClient(timeout=10.0, headers=UA, follow_redirects=True) as c:
        # 24h stats
        r = await c.get(f"https://api.exchange.coinbase.com/products/{product}/stats")
        r.raise_for_status()
        stats = r.json()
        # Spot price
        r2 = await c.get(f"https://api.coinbase.com/v2/prices/{product}/spot")
        r2.raise_for_status()
        price = float(r2.json()["data"]["amount"])
        open_24h = float(stats.get("open") or price)
        change = price - open_24h
        change_pct = (change / open_24h * 100) if open_24h else 0
        return {
            "price": price, "bid": price, "ask": price,
            "change": round(change, 5),
            "change_pct": round(change_pct, 4),
            "high": float(stats.get("high") or 0) or None,
            "low": float(stats.get("low") or 0) or None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


async def _coinbase_history(symbol_key: str) -> list:
    """1-year daily history from Coinbase Exchange in two ~6-month chunks (300-candle cap)."""
    product = COINBASE_PRODUCT_MAP.get(symbol_key)
    if not product:
        raise RuntimeError(f"Coinbase: {symbol_key} not mapped")
    now = datetime.now(timezone.utc)
    chunks = [
        (now - timedelta(days=365), now - timedelta(days=180)),
        (now - timedelta(days=180), now),
    ]
    out = []
    async with httpx.AsyncClient(timeout=15.0, headers=UA, follow_redirects=True) as c:
        for start, end in chunks:
            r = await c.get(
                f"https://api.exchange.coinbase.com/products/{product}/candles",
                params={
                    "granularity": 86400,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            )
            r.raise_for_status()
            candles = r.json()  # [[ts, low, high, open, close, volume], ...] newest-first
            for ts, lo, hi, op, cl, vol in candles:
                out.append({
                    "date": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "open": float(op), "high": float(hi), "low": float(lo),
                    "close": float(cl), "volume": float(vol),
                })
    # Dedupe by date and sort oldest-first
    by_date = {row["date"]: row for row in out}
    return [by_date[d] for d in sorted(by_date.keys())]


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


async def _yahoo_gold_history() -> list:
    """Yahoo Finance v8 chart API — Gold front-month futures (GC=F), 1y daily.

    Free, no key, OHLC, very reliable. This is the primary source for XAU history.
    """
    return await _yahoo_chart_history("GC=F", "gold")


async def _yahoo_chart_history(yh_symbol: str, label: str) -> list:
    """Generic Yahoo Finance v8 chart fetch — 1y of daily OHLC candles.
    Used for gold futures (GC=F) and equity indices (^DJI, ^NDX)."""
    from urllib.parse import quote as _urlquote
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{_urlquote(yh_symbol, safe='')}?range=1y&interval=1d")
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
        r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise RuntimeError(f"Yahoo returned no {label} candles")
    res = result[0]
    ts_list = res.get("timestamp") or []
    quotes = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quotes.get("open") or []
    highs = quotes.get("high") or []
    lows = quotes.get("low") or []
    closes = quotes.get("close") or []
    history: list = []
    for i, ts in enumerate(ts_list):
        close = closes[i] if i < len(closes) else None
        if close is None:
            continue
        date_str = datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d")
        history.append({
            "date": date_str,
            "open": float(opens[i]) if i < len(opens) and opens[i] is not None else float(close),
            "high": float(highs[i]) if i < len(highs) and highs[i] is not None else float(close),
            "low": float(lows[i]) if i < len(lows) and lows[i] is not None else float(close),
            "close": float(close),
            "volume": 0,
        })
    history.sort(key=lambda r: r["date"])
    return history


async def _yahoo_index_quote(yh_symbol: str) -> dict:
    """iter-43 — live index quote from the Yahoo v8 chart meta block."""
    from urllib.parse import quote as _urlquote
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{_urlquote(yh_symbol, safe='')}?range=1d&interval=5m")
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
        r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        data = r.json()
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise RuntimeError(f"Yahoo returned no quote for {yh_symbol}")
    meta = result[0].get("meta") or {}
    price = float(meta.get("regularMarketPrice") or 0)
    if price <= 0:
        raise RuntimeError(f"Yahoo returned zero price for {yh_symbol}")
    prev = float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
    change = round(price - prev, 2) if prev else 0.0
    return {
        "price": price, "bid": price, "ask": price,
        "change": change,
        "change_pct": round(change / prev * 100, 3) if prev else 0.0,
        "high": meta.get("regularMarketDayHigh"),
        "low": meta.get("regularMarketDayLow"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def _stooq_gold_history() -> list:
    """Stooq CSV — XAUUSD daily (may be JS-gated occasionally)."""
    url = "https://stooq.com/q/d/l/?s=xauusd&i=d"
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
        r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        text = r.text
    if "<html" in text.lower():
        raise RuntimeError("Stooq returned HTML (rate-limited)")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or "Date" not in lines[0]:
        raise RuntimeError("Stooq returned no CSV header")
    history: list = []
    for ln in lines[1:]:
        parts = ln.split(",")
        if len(parts) < 5:
            continue
        try:
            history.append({
                "date": parts[0],
                "open": float(parts[1]),
                "high": float(parts[2]),
                "low": float(parts[3]),
                "close": float(parts[4]),
                "volume": float(parts[5]) if len(parts) > 5 and parts[5] not in ("", "0") else 0,
            })
        except (ValueError, IndexError):
            continue
    history.sort(key=lambda r: r["date"])
    return history[-365:]


async def _gold_history() -> list:
    """Multi-source XAU/USD daily history with explicit fallback chain.

    Order: Yahoo (GC=F front-month, OHLC, reliable) → Stooq CSV → CoinGecko PAXG.
    Last resort is handled by the get_history() Mongo cache.
    """
    try:
        h = await _yahoo_gold_history()
        if len(h) >= 100:
            return h
    except Exception as e:
        log.warning("Yahoo gold history failed: %s", e)
    try:
        h = await _stooq_gold_history()
        if len(h) >= 100:
            return h
    except Exception as e:
        log.warning("Stooq gold history failed: %s", e)
    log.warning("Falling back to CoinGecko PAXG proxy for XAU history")
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
                try:
                    q = await _coinbase_quote(sym)
                except Exception:
                    q = await _cg_quote(meta["cg_id"])
            elif meta["asset"] == "commodity":
                q = await _gold_quote()
            elif meta["asset"] == "index":
                q = await _yahoo_index_quote(meta["yh"])
            elif meta["asset"] == "forex":
                q = await _fx_quote(meta["base"], meta["quote"])
            else:
                raise RuntimeError("Unknown asset class")
            result = {"symbol": sym, **q}
            _cache_set(cache_key, result, ttl_seconds_for_quote(meta["asset"]))
            # Persist into Mongo time-series collection for fast windowed queries
            try:
                from database import get_db
                from datetime import datetime, timezone
                db = get_db()
                await db.price_ticks.insert_one({
                    "ts": datetime.now(timezone.utc),
                    "symbol": sym,
                    "price": float(result.get("price") or 0),
                    "bid": float(result.get("bid") or 0),
                    "ask": float(result.get("ask") or 0),
                })
            except Exception:
                pass  # never block live quotes on persistence
            return {**result, "cached": False}
        except httpx.HTTPError as e:
            raise RuntimeError(f"Quote fetch failed for {sym}: {e}")


def ttl_seconds_for_quote(asset: str) -> int:
    # Live-tick goal: every Trades-page open position needs sub-10s quote
    # freshness so unrealized P&L matches the broker. The EA heartbeat (every
    # ~3-5s) is the broker-exact source — these polled fallback quotes are
    # only used when the EA isn't reporting that symbol yet.
    #   crypto    →  5s  (volatile)
    #   commodity →  5s  (was 120s — too stale for live UI)
    #   forex     → 60s  (FX moves slower, but we still need responsiveness)
    if asset == "crypto":
        return 5
    if asset == "commodity":
        return 5
    if asset == "index":
        return 15
    return 60


async def _history_save_to_mongo(sym: str, history: list) -> None:
    """Persist a successful history fetch so a future cold-start can recover
    even if every upstream is rate-limited."""
    try:
        from database import get_db
        await get_db().history_cache.update_one(
            {"_id": sym},
            {"$set": {
                "symbol": sym,
                "history": history,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "len": len(history),
            }},
            upsert=True,
        )
    except Exception as e:
        log.debug("history_save_to_mongo failed for %s: %s", sym, e)


async def _history_load_from_mongo(sym: str) -> list | None:
    """Last-resort retrieval if every live source is failing."""
    try:
        from database import get_db
        doc = await get_db().history_cache.find_one({"_id": sym})
        if not doc:
            return None
        hist = doc.get("history") or []
        if len(hist) < 100:
            return None
        log.warning("Using Mongo-cached history for %s (saved_at=%s, %d bars)",
                    sym, doc.get("saved_at"), len(hist))
        return hist
    except Exception as e:
        log.debug("history_load_from_mongo failed for %s: %s", sym, e)
        return None


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

        hist: list = []
        upstream_error: Exception | None = None
        try:
            if meta["asset"] == "crypto":
                try:
                    hist = await _coinbase_history(sym)
                    if len(hist) < 100:
                        raise RuntimeError("Insufficient Coinbase candles")
                except Exception:
                    hist = await _cg_history(meta["cg_id"])
            elif meta["asset"] == "commodity":
                hist = await _gold_history()
            elif meta["asset"] == "index":
                hist = await _yahoo_chart_history(meta["yh"], sym)
            elif meta["asset"] == "forex":
                hist = await _fx_history(meta["base"], meta["quote"])
        except (httpx.HTTPError, RuntimeError) as e:
            upstream_error = e

        if len(hist) >= 100:
            _cache_set(cache_key, hist, 21600)  # 6h memory cache
            await _history_save_to_mongo(sym, hist)
            return hist

        # Upstream failed or returned too few bars — fall back to Mongo
        fallback = await _history_load_from_mongo(sym)
        if fallback:
            # Cache for a short period (15 min) so we keep retrying upstream periodically
            _cache_set(cache_key, fallback, 900)
            return fallback

        raise RuntimeError(
            f"History fetch failed for {sym}: {upstream_error or 'no upstream returned enough bars'}"
        )


def _ttl_seconds_for_quote(symbol: str):  # backward compat alias
    return ttl_seconds_for_quote(asset_type_of(symbol))


# ---------- Indicators (re-exported from indicators module) ----------
from indicators import compute_indicators  # noqa: E402,F401
