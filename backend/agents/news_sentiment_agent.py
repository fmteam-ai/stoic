"""NewsSentimentAgent — score news flow for a symbol and digest the verdict.

Wraps the existing `news.score_sentiment` so the activity log gets a clean
one-line digest. Lean by design — the LLM-heavy interpretation already
happens inside `score_sentiment` (which also caches articles per-symbol).

Output:
  {
    "symbol": str,
    "score": float,           # -1 .. +1
    "label": "BULLISH"|"BEARISH"|"NEUTRAL"|"UNKNOWN",
    "article_count": int,
    "summary": str,
    "bias": str,              # one-liner for the activity log
  }
"""
import logging

from news import score_sentiment

logger = logging.getLogger("agent.news-sentiment")


def _label_from_score(score: float) -> str:
    if score >= 0.25:
        return "BULLISH"
    if score <= -0.25:
        return "BEARISH"
    return "NEUTRAL"


class NewsSentimentAgent:
    name = "news_sentiment"

    async def analyze(self, symbol: str) -> dict:
        sym = symbol.upper()
        try:
            s = await score_sentiment(sym)
        except Exception as e:  # noqa: BLE001
            logger.debug("score_sentiment failed: %s", e)
            return {"symbol": sym, "score": 0.0, "label": "UNKNOWN",
                    "article_count": 0, "summary": "news feed unavailable",
                    "bias": "unavailable"}

        score = float(s.get("score") or 0.0)
        label = (s.get("label") or _label_from_score(score)).upper()
        article_count = int(s.get("article_count") or 0)
        summary = s.get("summary") or ""

        bias = f"{label} ({score:+.2f}, n={article_count})"
        return {
            "symbol": sym,
            "score": score,
            "label": label,
            "article_count": article_count,
            "summary": summary,
            "bias": bias,
        }
