"""iter-106 · AI News Understanding Agent.

Instead of keyword sentiment: Claude (finance-strategist persona) reads EACH
headline — Reuters/Bloomberg wire, central-bank statements, FOMC, CPI, NFP —
and answers "how bullish is THIS specifically for <asset>?" on a -3..+3
scale. Per-headline scores are recency-weighted (12h half-life) into one net
score. `news_gate` vetoes trades against strongly-opposing news (|net| ≥ 2).
Cached 45 min per asset; every failure returns None (fail-open)."""
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx
from emergentintegrations.llm.chat import LlmChat, UserMessage

logger = logging.getLogger(__name__)

NEWSAPI_URL = "https://newsapi.org/v2/everything"
TIER1_DOMAINS = ("reuters.com,bloomberg.com,cnbc.com,ft.com,wsj.com,"
                 "marketwatch.com,investing.com,kitco.com")
MACRO_QUERY = ('FOMC OR "Federal Reserve" OR CPI OR "nonfarm payrolls" OR '
               '"central bank" OR "rate cut" OR "rate hike" OR '
               '"interest rate decision" OR "jobs report"')
CACHE_TTL = 45 * 60
FAIL_TTL = 10 * 60
RECENCY_HALF_LIFE_H = 12.0
NEWS_EXTREME = 2.0
MAX_HEADLINES = 12

_cache: dict = {}   # {base: (expires_ts, payload|None)}

ASSET_CONTEXT = {
    "XAUUSD": ("gold (XAUUSD). Weigh: USD strength, real yields, Fed policy path, "
               "inflation prints (CPI/PCE), NFP, safe-haven flows, geopolitical "
               "risk, central-bank gold buying"),
    "BTCUSD": ("Bitcoin (BTCUSD). Weigh: risk appetite, USD liquidity, ETF flows, "
               "regulation, Fed policy"),
    "US30": "the Dow Jones index (US30). Weigh: earnings, Fed policy, growth data, risk appetite",
    "NAS100": "the Nasdaq-100 index (NAS100). Weigh: tech earnings, yields, Fed policy, risk appetite",
}

ASSET_QUERY = {
    "XAUUSD": '"gold price" OR "gold market" OR "gold futures" OR bullion',
    "BTCUSD": "bitcoin",
    "US30": '"dow jones" OR "US stocks"',
    "NAS100": 'nasdaq OR "tech stocks"',
}


async def _fetch(client, params):
    r = await client.get(NEWSAPI_URL, params=params)
    if r.status_code != 200:
        return []
    return (r.json() or {}).get("articles") or []


async def fetch_market_news(base: str, limit: int = MAX_HEADLINES) -> list:
    """Macro wire (FOMC/CPI/NFP/central banks, tier-1 domains) + asset query."""
    key = os.environ.get("NEWSAPI_KEY", "")
    if not key:
        return []
    common = {"language": "en", "sortBy": "publishedAt", "apiKey": key,
              "searchIn": "title,description"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            macro = await _fetch(client, {**common, "q": MACRO_QUERY,
                                          "domains": TIER1_DOMAINS, "pageSize": 8})
            asset = await _fetch(client, {**common, "pageSize": 8,
                                          "q": ASSET_QUERY.get(base, base)})
    except Exception as e:
        logger.warning("news_understanding fetch failed: %s", e)
        return []
    seen, out = set(), []
    for a in macro + asset:
        title = (a.get("title") or "").strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        out.append({"title": title,
                    "source": (a.get("source") or {}).get("name") or "",
                    "publishedAt": a.get("publishedAt") or ""})
    return out[:limit]


def _recency_weight(published_at: str, now: datetime) -> float:
    try:
        dt = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_h = max(0.0, (now - dt).total_seconds() / 3600.0)
    except (ValueError, TypeError):
        age_h = 24.0
    return 0.5 ** (age_h / RECENCY_HALF_LIFE_H)


def _label(net: float) -> str:
    if net >= 2.0:
        return "strongly_bullish"
    if net >= 0.75:
        return "bullish"
    if net > -0.75:
        return "neutral"
    if net > -2.0:
        return "bearish"
    return "strongly_bearish"


def aggregate_scores(items: list, now: datetime | None = None) -> dict | None:
    """items: [{score, title, source, publishedAt, why}] → recency-weighted net."""
    scored = [it for it in items if it.get("score") is not None]
    if not scored:
        return None
    now = now or datetime.now(timezone.utc)
    wsum = ssum = 0.0
    for it in scored:
        w = _recency_weight(it.get("publishedAt", ""), now)
        s = max(-3.0, min(3.0, float(it["score"])))
        it["score"] = round(s, 1)
        wsum += w
        ssum += w * s
    net = round(max(-3.0, min(3.0, ssum / wsum)), 2) if wsum > 0 else 0.0
    drivers = sorted(scored, key=lambda x: abs(x["score"]), reverse=True)[:3]
    return {"net": net, "label": _label(net), "headlines": len(scored),
            "drivers": [{"title": d["title"][:120], "source": d.get("source", ""),
                         "score": d["score"], "why": d.get("why", "")}
                        for d in drivers]}


def _parse_array(text: str) -> list:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    m = re.search(r"\[.*\]", text, re.DOTALL)
    try:
        arr = json.loads(m.group(0) if m else text)
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


async def _score_headlines(base: str, heads: list) -> list:
    ctx = ASSET_CONTEXT.get(base, base)
    numbered = "\n".join(f"{i}. [{h['source']}] {h['title']}"
                         for i, h in enumerate(heads))
    prompt = (
        f"For EACH numbered headline below, answer: how bullish is this "
        f"specifically for {ctx}?\n"
        f"Score each from -3 (extremely bearish) to +3 (extremely bullish), "
        f"0 = irrelevant/no impact. Half-points allowed.\n"
        f'Respond ONLY with a JSON array: [{{"i": <index>, "score": <float>, '
        f'"why": "<max 10 words>"}}, ...] — one entry per headline.\n\n{numbered}')
    chat = LlmChat(
        api_key=os.environ["EMERGENT_LLM_KEY"],
        session_id=f"news-ai-{base}-{int(time.time())}",
        system_message=("You are a senior macro strategist at a bullion desk. "
                        "You read central-bank statements, FOMC minutes, CPI and "
                        "NFP prints for their asset-specific price impact. "
                        "Respond only with JSON."),
    ).with_model("anthropic", "claude-sonnet-4-5-20250929")
    raw = await chat.send_message(UserMessage(text=prompt))
    out = []
    for row in _parse_array(str(raw)):
        try:
            h = heads[int(row["i"])]
            out.append({**h, "score": float(row["score"]),
                        "why": str(row.get("why") or "")[:80]})
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return out


async def get_news_understanding(base: str) -> dict | None:
    now = time.time()
    hit = _cache.get(base)
    if hit and hit[0] > now:
        return hit[1]
    heads = await fetch_market_news(base)
    if not heads:
        _cache[base] = (now + FAIL_TTL, None)
        return None
    try:
        items = await _score_headlines(base, heads)
        snap = aggregate_scores(items)
    except Exception as e:
        logger.warning("news_understanding scoring failed: %s", e)
        snap = None
    if not snap:
        _cache[base] = (now + FAIL_TTL, None)
        return None
    snap["fetched_at"] = datetime.now(timezone.utc).isoformat()
    _cache[base] = (now + CACHE_TTL, snap)
    return snap


def news_gate(action: str, base: str, snap: dict | None) -> str | None:
    """Veto trades against strongly-opposing asset-specific news (|net| ≥ 2)."""
    if action not in ("BUY", "SELL") or not snap:
        return None
    net = float(snap.get("net") or 0)
    top = (snap.get("drivers") or [{}])[0].get("title", "")
    if action == "BUY" and net <= -NEWS_EXTREME:
        return (f"News gate: AI reads the tape as strongly bearish for {base} "
                f"(net {net:+.1f}/3 across {snap.get('headlines')} headlines) — "
                f"BUY vetoed. Top driver: {top}")
    if action == "SELL" and net >= NEWS_EXTREME:
        return (f"News gate: AI reads the tape as strongly bullish for {base} "
                f"(net {net:+.1f}/3 across {snap.get('headlines')} headlines) — "
                f"SELL vetoed. Top driver: {top}")
    return None
