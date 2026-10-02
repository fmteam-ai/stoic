"""News fetching (NewsAPI.org) + Claude-based sentiment scoring.

Asymmetric timeline: sentiment is event-driven, NOT tick-driven.
We fetch headlines + score sentiment at most once per hour per symbol,
then store the score for the technical AI to read instantly during signals.
"""
import os
import json
import re
import time
import asyncio
import httpx
import logging
from datetime import datetime, timezone, timedelta
from pydantic import BaseModel, Field

import llm_client
from llm_models import finite_float

logger = logging.getLogger("news")

NEWSAPI_URL = "https://newsapi.org/v2/everything"

# In-memory sentiment cache: { symbol: (expires_at, payload) }
_sentiment_cache: dict = {}
_sentiment_locks: dict = {}

# Search query per symbol — focused on price-moving headlines
QUERY_MAP = {
    "XAUUSD": "\"gold price\" OR \"gold market\" OR \"gold futures\"",
    "BTCUSD": "bitcoin",
    "ETHUSD": "ethereum",
    "SOLUSD": "solana",
    "BNBUSD": "\"binance coin\" OR BNB",
    "XRPUSD": "ripple xrp",
    "ADAUSD": "cardano",
    "DOGEUSD": "dogecoin",
    "EURUSD": "\"euro dollar\" OR \"EUR/USD\" OR ECB",
    "GBPUSD": "\"pound sterling\" OR \"GBP/USD\" OR \"bank of england\"",
    "USDJPY": "\"USD/JPY\" OR \"bank of japan\" OR \"japanese yen\"",
    "AUDUSD": "\"australian dollar\" OR \"AUD/USD\"",
    "USDCAD": "\"canadian dollar\" OR \"USD/CAD\"",
    "USDCHF": "\"swiss franc\" OR \"USD/CHF\"",
    "NZDUSD": "\"new zealand dollar\" OR \"NZD/USD\"",
}


def _cache_key(sym: str) -> str:
    return f"sentiment:{sym}"


def _lock_for(sym: str) -> asyncio.Lock:
    if sym not in _sentiment_locks:
        _sentiment_locks[sym] = asyncio.Lock()
    return _sentiment_locks[sym]


def _api_key() -> str:
    return os.environ.get("NEWSAPI_KEY", "")


async def fetch_headlines(symbol: str, limit: int = 8) -> list:
    """Pull recent (last 24h) price-relevant headlines for a symbol."""
    key = _api_key()
    if not key:
        return []
    q = QUERY_MAP.get(symbol.upper(), symbol)
    # NewsAPI dev tier returns articles delayed ~24h; widen the window to 48h.
    from_dt = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "q": q,
        "from": from_dt,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": limit,
        "searchIn": "title,description",
        "apiKey": key,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(NEWSAPI_URL, params=params)
        if r.status_code != 200:
            return []
        articles = (r.json() or {}).get("articles", [])
        return [
            {
                "title": a.get("title") or "",
                "source": (a.get("source") or {}).get("name") or "",
                "publishedAt": a.get("publishedAt") or "",
                "description": (a.get("description") or "")[:240],
            }
            for a in articles
            if a.get("title")
        ]


SENTIMENT_PROMPT = """You are a financial news sentiment analyst.
Given a list of recent headlines about a specific market, score the OVERALL sentiment for that market over the next 24h.

Output STRICT JSON only, no prose:
{"score": -1.0..1.0, "label": "very_bearish"|"bearish"|"neutral"|"bullish"|"very_bullish", "summary": "...", "key_drivers": ["...","..."]}

Scoring guide:
  -1.0 .. -0.6 = very_bearish     (panic, major negative shock)
  -0.6 .. -0.2 = bearish
  -0.2 ..  0.2 = neutral          (mixed / no strong signal)
   0.2 ..  0.6 = bullish
   0.6 ..  1.0 = very_bullish     (strong positive catalysts)

Summary: 1-2 sentences.
key_drivers: 2-4 short bullets (max 8 words each), citing actual headlines.
"""


class SentimentOut(BaseModel):
    """Structured-output schema. Ranges are enforced after the call
    (finite_float clamp), not by the schema, so a near-miss still parses."""
    score: float
    label: str
    summary: str = ""
    key_drivers: list[str] = Field(default_factory=list)


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except Exception:
        return {"score": 0.0, "label": "neutral", "summary": "Failed to parse model response.", "key_drivers": []}


async def score_sentiment(symbol: str) -> dict:
    """Return sentiment dict for a symbol. Cached 1h per symbol.

    Shape: { symbol, score: -1..1, label, summary, key_drivers, article_count, cached, fetched_at }
    """
    sym = symbol.upper()
    ck = _cache_key(sym)
    cached = _get_cache(ck)
    if cached:
        return {**cached, "cached": True}

    async with _lock_for(sym):
        cached = _get_cache(ck)
        if cached:
            return {**cached, "cached": True}

        headlines = await fetch_headlines(sym, limit=8)

        # No news → neutral
        if not headlines:
            payload = {
                "symbol": sym,
                "score": 0.0,
                "label": "neutral",
                "summary": "No recent news available; sentiment defaulted to neutral.",
                "key_drivers": [],
                "article_count": 0,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            _set_cache(ck, payload, 3600)
            return {**payload, "cached": False}

        # Everything that can fail (missing key, timeout, refusal, malformed
        # output) comes back as ok=False and degrades to NEUTRAL. A raise here
        # used to propagate into analyze_symbol and fail signal generation for
        # every user, uncached, so every tick retried the LLM.
        res = await llm_client.complete(
            feature="news_sentiment",
            system=SENTIMENT_PROMPT,
            user=f"Market: {sym}\nHeadlines (last 24h):",
            untrusted=[f"- [{h['source']}] {h['title']}" for h in headlines],
            schema=SentimentOut,
            max_tokens=600,
        )
        if res.ok:
            parsed = res.data.model_dump()
            failed = False
        else:
            logger.warning("news sentiment LLM failed for %s: %s", sym, res.error)
            parsed = {"score": 0.0, "label": "neutral", "summary": "Sentiment model failed.", "key_drivers": []}
            failed = True

        score = finite_float(parsed.get("score"), -1.0, 1.0, 0.0)
        label = parsed.get("label") or "neutral"

        payload = {
            "symbol": sym,
            "score": round(score, 3),
            "label": label,
            "summary": parsed.get("summary", ""),
            "key_drivers": parsed.get("key_drivers", []),
            "article_count": len(headlines),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        # Failures are cached briefly so an outage doesn't re-call the LLM on
        # every tick for every user.
        _set_cache(ck, payload, 300 if failed else 3600)
        return {**payload, "cached": False}


def _get_cache(k: str):
    item = _sentiment_cache.get(k)
    if not item:
        return None
    exp, val = item
    if time.time() > exp:
        _sentiment_cache.pop(k, None)
        return None
    return val


def _set_cache(k: str, v, ttl: int):
    _sentiment_cache[k] = (time.time() + ttl, v)
