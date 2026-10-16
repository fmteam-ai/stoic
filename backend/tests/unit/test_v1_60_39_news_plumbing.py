"""v1.60.39 — news plumbing (production check 15 Oct).

NEWSAPI_KEY is unset in production, so the legacy news.py path scored every symbol 0 "neutral". Sentiment is now
routed to news_understanding (RSS + AI), unavailable is reported honestly, entry snapshots (entry_context / dxy /
news_ai / sentiment) are persisted on the trade, and post-mortems read them."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_sentiment_unavailable_is_not_a_fake_neutral_zero(monkeypatch):
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    import news
    import news_understanding

    async def none(base):
        return None
    monkeypatch.setattr(news_understanding, "get_news_understanding", none)
    out = _run(news.score_sentiment("XAUUSD"))
    assert out["score"] is None and out["label"] == "unavailable" and out["available"] is False
    assert "unavailable" in out["summary"].lower()

    from ai_signals import _apply_dual_veto
    assert _apply_dual_veto("BUY", 80, out) == ("BUY", "")   # unavailable can never veto


def test_news_sentiment_agent_reports_unavailable(monkeypatch):
    from agents import news_sentiment_agent as mod

    async def unavailable(sym):
        return {"symbol": sym, "score": None, "label": "unavailable", "available": False, "summary": "no source"}
    monkeypatch.setattr(mod, "score_sentiment", unavailable)
    out = _run(mod.NewsSentimentAgent().analyze("XAUUSD"))
    assert out["label"] == "UNAVAILABLE" and out["score"] is None and out["available"] is False
    assert out["bias"].startswith("unavailable")

    async def live(sym):
        return {"symbol": sym, "score": -0.7, "label": "very_bearish", "article_count": 7, "summary": "x",
                "available": True, "source": "rss_ai"}
    monkeypatch.setattr(mod, "score_sentiment", live)
    out = _run(mod.NewsSentimentAgent().analyze("XAUUSD"))
    assert out["label"] == "VERY_BEARISH" and out["source"] == "rss_ai" and "n=7" in out["bias"]


def test_news_understanding_works_without_newsapi_and_tracks_sources(monkeypatch):
    """RSS wire alone feeds the scorer; aggregate_scores counts distinct sources for the 2-source veto rule."""
    monkeypatch.delenv("NEWSAPI_KEY", raising=False)
    import news_understanding as nu

    class R:
        status_code = 200
        text = ("<rss><channel><item><title>Iran strike lifts gold</title>"
                "<pubDate>Wed, 15 Oct 2025 08:00:00 GMT</pubDate></item></channel></rss>")

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            assert "newsapi" not in url
            return R()
    monkeypatch.setattr(nu.httpx, "AsyncClient", Client)
    heads = _run(nu.fetch_market_news("XAUUSD"))
    assert heads and heads[0]["title"] == "Iran strike lifts gold"
    assert nu._status["rss_items"] >= 1 and nu._status["newsapi_items"] == 0 and nu._status["last_error"] is None

    now = datetime.now(timezone.utc)
    snap = nu.aggregate_scores([{"title": "a", "source": "BBC", "score": 2, "publishedAt": now.isoformat()},
                                {"title": "b", "source": "CNBC", "score": 1, "publishedAt": now.isoformat()},
                                {"title": "c", "source": "bbc", "score": 0, "publishedAt": now.isoformat()}], now)
    assert snap["distinct_sources"] == 2 and snap["headlines"] == 3


def test_cached_news_ai_reads_mongo_within_ttl_and_never_fetches(monkeypatch):
    import news_understanding as nu
    db = FakeDb()
    monkeypatch.setattr("database.get_db", lambda: db)
    nu._cache.clear()

    async def boom(*a, **k):
        raise AssertionError("cache-only path must not fetch")
    monkeypatch.setattr(nu, "fetch_market_news", boom)
    assert _run(nu.cached_news_ai("XAUUSD")) is None
    fresh = {"_id": "XAUUSD", "net": 1.5, "label": "bullish", "headlines": 4,
             "fetched_at": datetime.now(timezone.utc).isoformat()}
    _run(db.news_ai_cache.insert_one(dict(fresh)))
    got = _run(nu.cached_news_ai("XAUUSD"))
    assert got and got["net"] == 1.5 and "_id" not in got
    _run(db.news_ai_cache.update_one({"_id": "XAUUSD"}, {"$set": {
        "fetched_at": (datetime.now(timezone.utc) - timedelta(seconds=nu.CACHE_TTL + 60)).isoformat()}}))
    assert _run(nu.cached_news_ai("XAUUSD")) is None


def test_persist_status_writes_platform_state_and_snapshot(monkeypatch):
    import news_understanding as nu
    db = FakeDb()
    monkeypatch.setattr("database.get_db", lambda: db)
    snap = {"net": -1.0, "label": "bearish", "headlines": 3, "fetched_at": datetime.now(timezone.utc).isoformat()}
    _run(nu._persist_status("XAUUSD", snap))
    st = _run(db.platform_state.find_one({"_id": "news_sources"}))
    assert st and st["rss_ai"]["base"] == "XAUUSD" and st["rss_ai"]["recorded_at"]
    assert _run(db.news_ai_cache.find_one({"_id": "XAUUSD"}))["net"] == -1.0


def test_entry_context_dxy_falls_back_to_cache_for_scalp(monkeypatch):
    import entry_context as ec
    db = FakeDb()
    monkeypatch.setattr("database.get_db", lambda: db)
    from datetime import datetime, timezone
    _run(db.dxy_cache.insert_one({"_id": "latest", "current_price": 103.42, "regime": "bullish",
                                  "fetched_at": datetime.now(timezone.utc).isoformat()}))   # v1.60.51: must be < 48 h old
    ctx = _run(ec.build_entry_context({"symbol": "XAUUSD", "action": "SELL", "strategy_engine": "scalp_fast"}))
    assert ctx["dxy"] == 103.42 and ctx["dxy_regime"] == "bullish"
    # a signal that already carries its own DXY snapshot wins
    ctx = _run(ec.build_entry_context({"symbol": "XAUUSD", "dxy": {"current_price": 99.0, "regime": "bearish"}}))
    assert ctx["dxy"] == 99.0


def test_execution_persists_entry_snapshots_on_trade():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "execution.py"), encoding="utf-8").read()
    i = src.index("trade_doc = {")
    body = src[i:i + 4000]
    for key in ('"entry_context": signal.get("entry_context")', '"dxy": _entry_dxy_value(signal)',
                '"news_ai": signal.get("news_ai") or await _cached_news_ai(base_symbol)',
                '"sentiment": signal.get("sentiment")'):
        assert key in body, key
    from execution import _entry_dxy_value
    assert _entry_dxy_value({"dxy": {"current_price": 101.2}}) == 101.2
    assert _entry_dxy_value({"dxy": 100.5}) == 100.5
    assert _entry_dxy_value({"entry_context": {"dxy": 99.9}}) == 99.9
    assert _entry_dxy_value({}) is None
    br = open(os.path.join(os.path.dirname(__file__), "..", "..", "bot_runner.py"), encoding="utf-8").read()
    assert '"news_ai": signal.get("news_ai"),' in br and '"dxy": signal.get("dxy"),' in br


def test_postmortem_reads_news_ai_and_dxy_from_signal_or_trade():
    from loss_postmortem import _quantitative_diff
    sig = {"news_ai": {"net": -1.8, "label": "bearish"}, "sentiment": {"score": 0.0},
           "dxy": {"current_price": 104.1}, "entry_context": {}}
    d = _quantitative_diff(sig, {}, {}, current_news={"net": 0.9})
    assert d["sentiment_at_entry"] == -1.8 and d["sentiment_source_at_entry"] == "news_ai"
    assert d["sentiment_now"] == 0.9 and d["sentiment_flipped"] is True
    assert d["dxy_at_entry"] == 104.1
    # legacy signal with only the NewsAPI-era sentiment; DXY stored as a plain number on the trade
    d = _quantitative_diff({"sentiment": {"score": 0.3}, "dxy": 102.0}, {}, {})
    assert d["sentiment_at_entry"] == 0.3 and d["sentiment_source_at_entry"] == "sentiment" and d["dxy_at_entry"] == 102.0
    # unavailable sentiment stays None instead of a fake 0 — and never "flips"
    d = _quantitative_diff({"sentiment": {"score": None, "label": "unavailable"}}, {}, {}, current_news={"net": 1.0})
    assert d["sentiment_at_entry"] is None and d["sentiment_source_at_entry"] is None and d["sentiment_flipped"] is False
    # entry_context.dxy is preferred when present
    d = _quantitative_diff({"entry_context": {"dxy": 98.5}, "dxy": {"current_price": 104.1}}, {}, {})
    assert d["dxy_at_entry"] == 98.5


def test_postmortem_merges_trade_snapshots_when_signal_lacks_them():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "loss_postmortem.py"), encoding="utf-8").read()
    assert 'for key in ("entry_context", "dxy", "news_ai", "sentiment"):' in src
    assert "cached_news_ai" in src and "_quantitative_diff(signal, cur_macro, cur_session, cur_news)" in src


def test_ops_news_sources_endpoint_exists_and_bot_health_renders_card():
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    ops = open(os.path.join(root, "backend", "routes", "ops_routes.py"), encoding="utf-8").read()
    assert '@router.get("/ops/news-sources")' in ops and "newsapi_configured()" in ops
    bh = open(os.path.join(root, "frontend", "src", "pages", "BotHealth.jsx"), encoding="utf-8").read()
    assert "<NewsSourcesCard />" in bh
    card = open(os.path.join(root, "frontend", "src", "components", "NewsSourcesCard.jsx"), encoding="utf-8").read()
    for tid in ("news-source-calendar", "news-source-rss-ai", "news-source-newsapi"):
        assert tid in card
