"""v1.60.53 — data sources (Finnhub, GDELT, central banks, GVZ/VIX) ADVISORY/SHADOW + broker-price trade management."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND = os.path.join(os.path.dirname(BACKEND), "frontend", "src")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ----------------------------------------------------------------------------------------------- A. Finnhub
def test_finnhub_key_loader_never_leaks_and_disabled_without_key(tmp_path, monkeypatch):
    from providers import finnhub as fh
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    monkeypatch.delenv("FINNHUB_API_KEY_FILE", raising=False)
    assert fh.configured() is False
    st = fh.status()
    assert st["configured"] is False and st["level"] == "info" and "not configured" in st["message"]
    assert _run(fh.fetch_calendar()) is None and _run(fh.fetch_news()) == []
    f = tmp_path / "k"
    f.write_text("sekrit-123\n")
    monkeypatch.setenv("FINNHUB_API_KEY_FILE", str(f))
    assert fh.configured() and fh.api_key() == "sekrit-123"
    assert "sekrit" not in str(fh.status())
    monkeypatch.setenv("FINNHUB_API_KEY", "envkey")
    assert fh.api_key() == "envkey", "env wins over file"


def test_finnhub_calendar_mapping_surprise_cadence_and_plan_restriction():
    from providers import finnhub as fh
    rows = {"economicCalendar": [
        {"country": "US", "event": "CPI m/m", "impact": 3, "estimate": 0.3, "actual": 0.4, "prev": 0.2, "time": "2026-10-16 12:30:00"},
        {"country": "GB", "event": "GDP", "impact": "high", "estimate": 0.1, "actual": None, "prev": 0.0, "time": "2026-10-16 06:00:00"},
        {"country": "ZA", "event": "Rand thing", "impact": 2, "estimate": None, "actual": None, "prev": None, "time": "2026-10-16 09:00:00"},
        {"country": "DE", "event": "ZEW", "impact": 1, "estimate": 10, "actual": 5, "prev": 8, "time": "2026-10-16 09:00:00"},
    ]}
    evts = fh.parse_calendar(rows)
    by = {e["title"]: e for e in evts}
    assert by["CPI m/m"]["country"] == "USD" and by["CPI m/m"]["impact"] == "high" and by["CPI m/m"]["source"] == "finnhub"
    assert by["GDP"]["country"] == "GBP" and by["GDP"]["surprise"] is None and by["GDP"]["actual"] == ""
    assert by["Rand thing"]["country"] == "OTHER", "unknown country is never USD / ALL (bug #4)"
    assert by["ZEW"]["country"] == "EUR"
    sp = by["CPI m/m"]["surprise"]
    assert abs(sp["raw"] - 0.1) < 1e-9 and abs(sp["pct"] - 0.3333) < 1e-3 and sp["method"] == "pct"
    z = fh.surprise(0.5, 0.3, history=[0.0, 0.1, -0.1, 0.05, 0.0, 0.02])
    assert z["method"] == "z" and z["z"] > 2
    now = datetime(2026, 10, 16, 12, 25, tzinfo=timezone.utc).timestamp()
    assert fh.calendar_interval_sec(evts, now) == 30                 # ±10 min of the high-impact CPI
    assert fh.calendar_interval_sec(evts, now - 3600) == 300
    # 403 → logged once, endpoint unavailable, no crash; the calendar module keeps the other sources
    fh._state["unavailable"].clear()
    fh._mark_unavailable("calendar/economic", 403)
    fh._mark_unavailable("calendar/economic", 403)
    assert fh._state["unavailable"]["calendar/economic"]["status"] == 403
    with patch.object(fh, "configured", lambda: True):
        assert _run(fh.fetch_calendar()) is None
    fh._state["unavailable"].clear()
    import economic_calendar as ec
    assert ec.SOURCE_ORDER[0] == "finnhub" and "finnhub" in ec._sources
    merged = ec.merge_events([{**by["CPI m/m"]}], [{"title": "CPI m/m", "country": "USD", "when_ts": by["CPI m/m"]["when_ts"],
                                                  "impact": "high", "source": "forexfactory"}])
    assert len(merged) == 1 and merged[0]["source"] == "finnhub" and merged[0]["actual"] == "0.4"


def test_finnhub_news_dedupe_tagging_relevance_and_backoff():
    from providers import finnhub as fh
    rows = [{"headline": "Fed holds rates, Powell signals patience", "summary": "", "url": "https://x/1", "source": "Reuters", "datetime": 1760000000},
            {"headline": "Gold jumps as yields slide", "summary": "bullion", "url": "https://x/2", "source": "CNBC", "datetime": 1760000100},
            {"headline": "Celebrity opens restaurant", "summary": "food", "url": "https://x/3", "source": "People", "datetime": 1760000200}]
    items = fh.parse_news(rows, "general")
    assert items[0]["currencies"] == ["USD"] and items[0]["relevant"]
    assert "XAU" in items[1]["currencies"] and items[1]["relevant"]
    assert items[2]["relevant"] is False and items[2]["currencies"] == []
    known = {fh.news_fingerprint({"url": "https://x/1"})}
    assert [i["title"] for i in fh.dedupe(items, known)] == [items[1]["title"], items[2]["title"]]
    assert fh.news_fingerprint({"title": "Gold  jumps as yields slide"}) == fh.news_fingerprint({"title": "gold jumps as yields slide"})

    class _R:
        status_code = 429
        text = ""
    fh._state.update({"backoff_until": 0.0, "backoff_sec": 30.0})

    class _C:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _R()
    with patch.object(fh, "api_key", lambda: "k"), patch.object(fh.httpx, "AsyncClient", _C):
        assert _run(fh._get("news", {"category": "forex"})) is None
    assert fh._state["backoff_until"] > 0 and fh._state["backoff_sec"] == 60.0
    fh._state.update({"backoff_until": 0.0, "backoff_sec": 30.0})
    assert "finnhub_items" in open(os.path.join(BACKEND, "news_understanding.py")).read()


# ----------------------------------------------------------------------------------------------- B. risk regime (shadow)
def test_geo_risk_is_a_zscore_against_baseline_and_risk_off_score_combines_components():
    import risk_regime as rr
    hist = [{"volume": 100 + (i % 7)} for i in range(60)]
    calm = rr.geo_component(103, -1.0, hist)
    spike = rr.geo_component(160, -6.0, hist)
    assert calm["score"] < spike["score"] and spike["z"] > 5 and spike["score"] >= 90
    assert rr.geo_component(500, None, [])["z"] is None and rr.geo_component(500, None, [])["score"] == 50.0, "no baseline → neutral, raw counts never score"
    n, tone = rr.parse_gdelt_artlist({"articles": [{"tone": "-3.5"}, {"tone": "1.5"}, {"tone": "x"}]})
    assert n == 3 and tone == -1.0
    s = rr.risk_off_score(80.0, "stressed", "elevated", {"minutes_since": 10, "unscheduled": False})
    assert 60 < s["risk_off_score"] <= 100 and s["bucket"] == "high" and s["top_drivers"][0] == "geo_risk"
    quiet = rr.risk_off_score(20.0, "calm", "calm", None)
    assert quiet["bucket"] == "low" and quiet["components"]["cb_event"] is None and "cb_event" in quiet["missing"]
    assert rr.risk_off_score(None, None, None, None)["risk_off_score"] == 0.0
    rss = '<rss><channel><item><title>FOMC statement: rate decision</title><link>https://fed/1</link><pubDate>x</pubDate></item>' \
          '<item><title>Speech by Governor</title><link>https://fed/2</link></item></channel></rss>'
    items = rr.parse_cb_rss(rss, "USD", "Fed")
    assert items[0]["kind"] == "decision" and items[1]["kind"] == "release" and "summary" not in items[0]


def test_risk_regime_is_stamped_shadow_only_and_flag_has_no_gate():
    import risk_regime as rr
    from models import BotConfigUpdate
    assert BotConfigUpdate().geo_risk_gate_enabled is False
    db = FakeDb()
    db.fred_cache.rows.append({"_id": "snapshot", "payload": {"series": {
        "VIXCLS": {"value": 30, "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "vol_regime": "stressed"},
        "GVZCLS": {"value": 20, "date": "2020-01-01", "vol_regime": "stressed"}}}})     # GVZ stale → ignored
    doc = _run(rr.build_advisory(db, {"geo_risk": 70.0, "regions": {"global": {"score": 70.0}}, "top_regions": []}))
    assert doc["shadow"] is True and doc["vix_regime"] == "stressed" and doc["gvz_regime"] is None
    assert len(db.risk_regime_advisory.rows) == 1
    stamp = rr.stamp_for_entry(_run(rr.latest_advisory(db)))
    assert stamp["evaluated"] and stamp["risk_off_score"] == doc["risk_off_score"] and stamp["risk_off_bucket"] == doc["bucket"]
    assert rr.stamp_for_entry(None) == {"risk_off_score": None, "risk_off_bucket": None, "evaluated": False}
    # the flag is plumbing only: no runtime reads it as a gate
    for rel in ("bot_runner.py", "execution.py", "trade_manager.py", "scalp/engine.py", "position_protector.py"):
        src = open(os.path.join(BACKEND, rel)).read()
        assert "geo_risk_gate_enabled" not in src, rel
    assert "risk_off_score" not in open(os.path.join(BACKEND, "bot_runner.py")).read()
    ec = open(os.path.join(BACKEND, "entry_context.py")).read()
    assert 'ctx["risk_regime"] = stamp_for_entry(' in ec
    trades = [{"pnl": 10, "r_multiple": 1.0, "entry_context": {"risk_regime": {"risk_off_bucket": "low"}}},
              {"pnl": -5, "r_multiple": -1.0, "entry_context": {"risk_regime": {"risk_off_bucket": "low"}}},
              {"pnl": -8, "r_multiple": -1.0, "entry_context": {"risk_regime": {"risk_off_bucket": "high"}}},
              {"pnl": 3, "risk_usd": 6}]
    ev = {b["bucket"]: b for b in rr.bucket_evaluation(trades)}
    assert ev["low"]["n"] == 2 and ev["low"]["win_rate"] == 50.0 and ev["low"]["expectancy_r"] == 0.0
    assert ev["high"]["win_rate"] == 0.0 and ev["unknown"]["expectancy_r"] == 0.5 and ev["med"]["n"] == 0
    assert 'data-testid="risk-regime-card"' in open(os.path.join(FRONTEND, "components", "RiskRegimeCard.jsx")).read()
    assert 'strategy-report-risk-off' in open(os.path.join(FRONTEND, "components", "StrategyReport.jsx")).read()


# ----------------------------------------------------------------------------------------------- C. GVZ / VIX
def test_gvz_vix_regimes_and_staleness():
    from macro.fred import SERIES, vol_regime, vol_series_stale, vol_context
    assert "GVZCLS" in SERIES and "VIXCLS" in SERIES
    base = [15.0 + (i % 5) * 0.2 for i in range(40)]
    assert vol_regime(base + [14.0])["vol_regime"] == "calm"
    assert vol_regime(base + [15.5])["vol_regime"] == "normal"
    assert vol_regime(base + [15.65])["vol_regime"] == "elevated"
    stressed = vol_regime(base + [22.0])
    assert stressed["vol_regime"] == "stressed" and stressed["pct_20d"] == 100.0 and stressed["change_5d"] > 5
    assert vol_regime([])["vol_regime"] is None
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)                        # Friday
    assert vol_series_stale({"date": "2026-10-15"}, now) is False
    assert vol_series_stale({"date": "2026-10-09"}, now) is True                 # 5 trading days back
    assert vol_series_stale({"date": "2026-10-12"}, now) is False                # 4 trading days — the limit
    assert vol_series_stale(None, now) is True
    ctx = vol_context({"GVZCLS": {"value": 20, "date": "2026-10-15", "vol_regime": "elevated"},
                       "VIXCLS": {"value": 30, "date": "2026-10-01", "vol_regime": "stressed"}}, now)
    assert ctx["gvz_regime"] == "elevated" and ctx["vix_regime"] is None and ctx["vix_stale"] is True
    page = open(os.path.join(FRONTEND, "pages", "Agents.jsx")).read()
    assert '"GVZCLS"' in page and "macro-${k}-vol-regime" in page
    assert '"code": f"{_k}_stale"' in open(os.path.join(BACKEND, "routes", "bot_routes.py")).read()


# ----------------------------------------------------------------------------------------------- D. broker price
def test_broker_price_direction_correct_fresh_fallback_and_skip():
    import broker_price as bp
    now_ms = 1_760_000_000_000
    at = datetime.fromtimestamp((now_ms - 5000) / 1000, tz=timezone.utc).isoformat()
    buy = {"action": "BUY", "live_price": 4000.5, "live_bid": 4000.3, "live_ask": 4000.7, "live_price_at": at}
    snap = bp.for_trade(buy, now_ms=now_ms)
    assert snap["fresh"] and snap["source"] == "broker" and snap["price"] == 4000.3 and snap["used"] == "bid" and snap["age_ms"] == 5000
    sell = {**buy, "action": "SELL"}
    assert bp.for_trade(sell, now_ms=now_ms)["price"] == 4000.7 and bp.for_trade(sell, now_ms=now_ms)["used"] == "ask"
    only = {"action": "BUY", "live_price": 4000.5, "live_at": at}                          # legacy: current_price only
    s2 = bp.for_trade(only, now_ms=now_ms)
    assert s2["price"] == 4000.5 and s2["used"] == "current_price" and "current_price" in s2["note"]
    stale = {**buy, "live_price_at": datetime.fromtimestamp((now_ms - 40000) / 1000, tz=timezone.utc).isoformat()}
    s3 = bp.for_trade(stale, now_ms=now_ms)
    assert s3["fresh"] is False and s3["age_ms"] == 40000, "stale → skip, never a web quote"
    tick = {"bid": 4001.0, "ask": 4001.4, "ts_ms": now_ms - 2000}
    s4 = bp.for_trade(stale, scalp_tick=tick, now_ms=now_ms)
    assert s4["fresh"] and s4["source"] == "scalp_tick" and s4["price"] == 4001.0
    assert bp.for_trade({"action": "BUY"}, now_ms=now_ms)["fresh"] is False
    h = bp.health_for_trades([{**buy, "status": "open", "mt5_ticket": 1, "account_id": "A"},
                              {**stale, "status": "open", "mt5_ticket": 2, "account_id": "B",
                               "live_price_at": datetime.fromtimestamp((now_ms - 90000) / 1000, tz=timezone.utc).isoformat()},
                              {"action": "BUY", "status": "open", "mode": "paper", "mt5_ticket": 3, "account_id": "C"}], now_ms)
    assert h["A"]["amber"] is False and h["B"]["amber"] is True and h["B"]["max_age_ms"] == 90000 and "C" not in h


def test_trade_manager_uses_broker_price_and_paper_path_unchanged():
    tm = open(os.path.join(BACKEND, "trade_manager.py")).read()
    body = tm[tm.index("async def _manage_one_trade"):]
    assert "snap = await _broker_snapshot(trade)" in body and "_bp.for_trade(trade, await _scalp_tick_for(trade))" in tm
    assert 'if not snap.get("fresh"):' in body and "_management_skipped_pulse" in body
    assert "get_quote(" not in body, "live tier management never calls the web quote"
    assert "broker price stale — management skipped" in tm
    ex = open(os.path.join(BACKEND, "execution.py")).read()
    assert 'get_quote(signal["symbol"], allow_stale=False)' in ex and "get_quote(sym, allow_stale=False)" in ex   # paper path
    br = open(os.path.join(BACKEND, "routes", "bridge_routes.py")).read()
    assert '"live_price_at": now_iso' in br and 'live_{_k}' in br
    assert '"broker_price": _broker_price_health' in open(os.path.join(BACKEND, "routes", "bot_routes.py")).read()
