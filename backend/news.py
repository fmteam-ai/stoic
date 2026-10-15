"""Symbol sentiment for the signal path — `signal["sentiment"]`.

v1.60.39 — routed to `news_understanding` (real-time RSS wire + per-headline LLM scoring), which works in
production without NEWSAPI_KEY. The legacy NewsAPI path (free tier delays articles up to 24 h) only ever
supplements headlines inside news_understanding when the key happens to be set. When no source is available the
payload says so (score None, label "unavailable") instead of a fake neutral 0.

Shape: { symbol, score: -1..1 | None, label, summary, key_drivers, article_count, distinct_sources,
         available, source, fetched_at }
"""
import logging
import re
from datetime import datetime, timezone

from pip_utils import base_symbol

logger = logging.getLogger("news")

_LABELS = ("very_bearish", "bearish", "neutral", "bullish", "very_bullish")
_NET_LABEL = {"strongly_bearish": "very_bearish", "bearish": "bearish", "neutral": "neutral",
              "bullish": "bullish", "strongly_bullish": "very_bullish"}
UNAVAILABLE_SUMMARY = "News sentiment unavailable — no live source (RSS wire unreachable or AI scoring failed; NewsAPI off)."


def _clean(v, limit: int) -> str:
    """Untrusted headline text → single line, printable, capped."""
    t = re.sub(r"[\x00-\x1f\x7f`]+", " ", str(v or ""))
    return re.sub(r"\s+", " ", t).strip()[:limit]


def sanitize_sentiment(parsed) -> dict:
    """Coerce a model answer into {score, label, summary, key_drivers} (score clamped to [-1, 1], label from the
    known set, drivers ≤ 4 short strings)."""
    if not isinstance(parsed, dict):
        parsed = {}
    try:
        score = float(parsed.get("score"))
        if score != score or score in (float("inf"), float("-inf")):
            raise ValueError
    except (TypeError, ValueError):
        score = 0.0
    score = max(-1.0, min(1.0, score))
    label = str(parsed.get("label") or "").strip().lower().replace(" ", "_")
    if label not in _LABELS:
        label = ("very_bearish" if score <= -0.6 else "bearish" if score <= -0.2 else
                 "very_bullish" if score >= 0.6 else "bullish" if score >= 0.2 else "neutral")
    drivers = parsed.get("key_drivers")
    if not isinstance(drivers, list):
        drivers = []
    drivers = [_clean(d, 120) for d in drivers if isinstance(d, (str, int, float))][:4]
    return {"score": round(score, 3), "label": label,
            "summary": _clean(parsed.get("summary"), 400), "key_drivers": drivers}


def unavailable_sentiment(symbol: str) -> dict:
    return {"symbol": symbol.upper(), "score": None, "label": "unavailable", "summary": UNAVAILABLE_SUMMARY,
            "key_drivers": [], "article_count": 0, "distinct_sources": 0, "available": False, "source": None,
            "fetched_at": datetime.now(timezone.utc).isoformat()}


def sentiment_from_understanding(symbol: str, snap: dict | None) -> dict:
    """news_understanding snapshot (net -3..+3, drivers) → legacy sentiment shape (score -1..+1)."""
    if not snap or snap.get("net") is None:
        return unavailable_sentiment(symbol)
    net = max(-3.0, min(3.0, float(snap.get("net") or 0.0)))
    drivers = [d for d in (snap.get("drivers") or []) if isinstance(d, dict)]
    summary = "; ".join(f"{_clean(d.get('title'), 90)} ({float(d.get('score') or 0):+.1f})" for d in drivers[:2])
    body = sanitize_sentiment({"score": net / 3.0, "label": _NET_LABEL.get(str(snap.get("label") or ""), ""),
                               "summary": summary or f"AI net read {net:+.2f}/3 across {snap.get('headlines')} headlines",
                               "key_drivers": [d.get("why") or d.get("title") for d in drivers]})
    return {"symbol": symbol.upper(), **body, "net": net,
            "article_count": int(snap.get("headlines") or 0),
            "distinct_sources": int(snap.get("distinct_sources") or 0),
            "available": True, "source": "rss_ai",
            "fetched_at": snap.get("fetched_at") or datetime.now(timezone.utc).isoformat()}


async def score_sentiment(symbol: str) -> dict:
    """Sentiment for a (possibly broker-suffixed) symbol via the RSS + AI wire. Cached inside news_understanding."""
    sym = symbol.upper()
    base = base_symbol(sym) or sym
    try:
        from news_understanding import get_news_understanding
        snap = await get_news_understanding(base)
    except Exception as e:  # noqa: BLE001 — sentiment must never break a signal
        logger.debug("news understanding failed for %s: %s", base, e)
        snap = None
    return sentiment_from_understanding(sym, snap)
