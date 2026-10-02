"""iter-60 · Macro Agent Fed-tone scoring — Claude reads Fed/FOMC headlines
and scores hawkish(+1) ↔ dovish(-1). Cached 6h; failures return None so the
signal loop never blocks on it. Extreme tone (|score| ≥ 0.7) gates XAUUSD."""
import logging
import os
import time

import httpx
from pydantic import BaseModel

import llm_client
from llm_models import finite_float

logger = logging.getLogger(__name__)

class FedToneOut(BaseModel):
    score: float            # clamped to [-1, 1] by finite_float after the call
    label: str
    summary: str = ""


_SYSTEM = "You are a monetary policy analyst."

CACHE_TTL = 6 * 3600
FED_TONE_EXTREME = 0.7
_cache: dict = {}   # {"exp": ts, "payload": {...}}


async def _fed_headlines(limit: int = 8) -> list:
    key = os.environ.get("NEWSAPI_KEY", "")
    if not key:
        return []
    params = {
        "q": '"Federal Reserve" OR FOMC OR "Jerome Powell" OR "Fed rate"',
        "language": "en", "sortBy": "publishedAt", "pageSize": limit,
        "apiKey": key,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://newsapi.org/v2/everything", params=params)
            r.raise_for_status()
            arts = (r.json() or {}).get("articles") or []
            return [{"title": a.get("title") or "",
                     "source": (a.get("source") or {}).get("name") or ""}
                    for a in arts if a.get("title")]
    except Exception as e:
        logger.warning("fed_tone headlines fetch failed: %s", e)
        return []


async def get_fed_tone() -> dict | None:
    now = time.time()
    if _cache.get("exp", 0) > now:
        return _cache.get("payload")
    heads = await _fed_headlines()
    if not heads:
        _cache.update(exp=now + 900, payload=None)
        return None
    prompt = (
        "Score the Federal Reserve policy tone from these headlines on a scale "
        "from -1.0 (extremely dovish: cuts, easing, stimulus) to +1.0 (extremely "
        "hawkish: hikes, tightening, higher-for-longer). label is one of "
        "hawkish|dovish|neutral; summary is one sentence."
    )
    res = await llm_client.complete(
        feature="fed_tone", system=_SYSTEM, user=prompt,
        untrusted=[f"- {h['title']} ({h['source']})" for h in heads[:8]],
        schema=FedToneOut, max_tokens=400)
    if not res.ok:
        logger.warning("fed_tone scoring failed: %s", res.error)
        _cache.update(exp=now + 900, payload=None)
        return None
    payload = {"score": finite_float(res.data.score, -1.0, 1.0, 0.0),
               "label": str(res.data.label or "neutral"),
               "summary": str(res.data.summary or ""),
               "headlines": len(heads)}
    _cache.update(exp=now + CACHE_TTL, payload=payload)
    return payload


def fed_tone_gate(action: str, symbol_base: str, tone: dict | None) -> str | None:
    if action not in ("BUY", "SELL") or symbol_base != "XAUUSD" or not tone:
        return None
    score = float(tone.get("score") or 0)
    if action == "BUY" and score >= FED_TONE_EXTREME:
        return (f"Fed-tone gate: extremely hawkish Fed headlines (score {score:+.2f}) "
                f"— strong-USD pressure, gold BUY vetoed. {tone.get('summary', '')}")
    if action == "SELL" and score <= -FED_TONE_EXTREME:
        return (f"Fed-tone gate: extremely dovish Fed headlines (score {score:+.2f}) "
                f"— weak-USD pressure, gold SELL vetoed. {tone.get('summary', '')}")
    return None
