"""#39 shared anti-tilt helper · #40 R-multiple contract size · #41 Market Intelligence card (display only)."""
import asyncio
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND = os.path.join(os.path.dirname(BACKEND), "frontend", "src")
NOW = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ------------------------------------------------------------------------------------------------ #40 R-multiple
def _gold(symbol, pnl, lot=0.01, entry=4000.0, sl=3990.0):
    return {"symbol": symbol, "entry_price": entry, "stop_loss": sl, "lot_size": lot, "pnl": pnl}


@pytest.mark.parametrize("symbol", ["GOLD#", "XAUUSD+", "XAUUSD.crp", "XAUUSD"])
def test_r_multiple_normalises_broker_gold_symbols(symbol):
    """OnEquity GOLD# / STARTRADER XAUUSD+ / XAUUSD.crp: contract 100 → $10 move × 0.01 lot = $1 risk per $1/oz."""
    from analytics import _r_multiple
    # risk = 10 × 0.01 × 100 = $10 → −$9.55 is −0.955R (was contract 1 → −95.5R → clamped −10R)
    assert _r_multiple(_gold(symbol, -9.55)) == pytest.approx(-0.955)
    assert _r_multiple(_gold(symbol, -1.12)) == pytest.approx(-0.112)


def test_r_multiple_forex_uses_100k_contract():
    from analytics import _r_multiple
    # EURUSD 10 pips SL (0.0010) × 0.10 lot × 100,000 = $10 risk → −$5 = −0.5R
    assert _r_multiple({"symbol": "EURUSD", "entry_price": 1.1000, "stop_loss": 1.0990, "lot_size": 0.10, "pnl": -5.0}) == pytest.approx(-0.5)
    assert _r_multiple({"symbol": "EURUSD#", "entry_price": 1.1000, "stop_loss": 1.0990, "lot_size": 0.10, "pnl": 10.0}) == pytest.approx(1.0)


def test_r_multiple_unknown_contract_returns_none_not_clamped():
    from analytics import _r_multiple
    from instruments import known_contract_size
    assert known_contract_size("WEIRDSYM") is None
    assert _r_multiple({"symbol": "WEIRDSYM", "entry_price": 100, "stop_loss": 90, "lot_size": 1, "pnl": -5}) is None


def test_r_multiple_prefers_account_broker_spec():
    from analytics import _r_multiple
    from instruments import known_contract_size
    acct = {"symbol_specs": {"XAUUSD": {"contract_size": 10.0}}}     # mini-gold contract reported by the EA
    assert known_contract_size("GOLD#", acct) == 10.0
    assert _r_multiple(_gold("GOLD#", -1.0), acct) == pytest.approx(-1.0)   # risk = 10 × 0.01 × 10 = $1
    assert known_contract_size("XAUUSD", {"symbol_specs": {"XAUUSD": {"point": 0.01}}}) == 100.0   # spec without size → table


def test_trade_r_multiple_uses_original_stop_and_volume():
    from analytics import trade_r_multiple
    t = {**_gold("GOLD#", -9.55), "stop_loss": 4000.0, "original_stop_loss": 3990.0, "lot_size": 0.005, "original_lot_size": 0.01}
    assert trade_r_multiple(t) == pytest.approx(-0.955)


def test_streak_loss_on_broker_gold_symbol_is_a_scratch_not_a_loss():
    from streak_losses import classify_loss, is_streak_loss
    # −$0.50 on a $10 planned risk = −0.05R → scratch (previously contract 1 → −5R → "loss")
    c = classify_loss(_gold("GOLD#", -0.50))
    assert c["counts"] is False and c["r"] == pytest.approx(-0.05) and "scratch" in c["why"]
    assert is_streak_loss(_gold("GOLD#", -9.55)) is True
    assert "planned risk unknown" in classify_loss({"pnl": -0.5})["why"]


def test_recompute_script_dry_run_by_default():
    src = open(os.path.join(BACKEND, "scripts", "recompute_r_multiples.py")).read()
    assert 'ap.add_argument("--apply", action="store_true"' in src and "trade_r_multiple" in src
    assert '"pnl"' not in src.split("$set")[1].split("}")[0]   # never writes pnl


# ------------------------------------------------------------------------------------------------ #39 anti-tilt
def _closed(pnl, mins_ago, origin="auto", symbol="GOLD#", **extra):
    return {"_id": ObjectId(), "user_id": "u1", "account_id": "a1", "status": "closed", "origin": origin, "symbol": symbol,
            "pnl": pnl, "closed_at": (NOW - timedelta(minutes=mins_ago)).isoformat(), "entry_price": 4000.0,
            "stop_loss": 3990.0, "lot_size": 0.01, **extra}


def _cfg(**kw):
    return {"_id": ObjectId(), "user_id": "u1", "account_id": "a1", "anti_tilt_enabled": True,
            "anti_tilt_consecutive_losses": 3, "anti_tilt_freeze_hours": 4, **kw}


def test_anti_tilt_old_banner_rule_no_longer_freezes_on_manual_or_scratch_trades():
    """16 Oct: Bot Health said 'freeze 46m' (all(pnl<0) over ANY trades) while the runner had 0 blocks."""
    from anti_tilt import evaluate
    db = FakeDb()
    db.trades.rows = [_closed(-0.30, 10), _closed(-0.16, 20, origin="manual"), _closed(-9.5, 30), _closed(-9.5, 40)]
    out = _run(evaluate(db, "u1", _cfg(), NOW))
    assert out["active"] is False                       # newest auto trade is a −0.03R scratch → streak broken
    assert [t["counts_as_loss"] for t in out["trades"]][:3] == [False, True, True]
    assert all(t["symbol"] for t in out["trades"]) and "scratch" in out["trades"][0]["why"]
    assert out["loss_streak"] == 0 and "origin=auto" in out["rule"]


def test_anti_tilt_freezes_on_three_real_auto_losses_and_lists_why():
    from anti_tilt import evaluate
    db = FakeDb()
    db.trades.rows = [_closed(-9.5, 10), _closed(-9.6, 20), _closed(-9.7, 30), _closed(+5.0, 40)]
    out = _run(evaluate(db, "u1", _cfg(), NOW))
    assert out["active"] is True and out["remaining_seconds"] == pytest.approx(4 * 3600 - 600, abs=2)
    assert out["eta"] == "3h 50m" and out["reason"].startswith("Anti-tilt freeze — last 3 trades lost")
    assert all(t["counts_as_loss"] and "gave back" in t["why"] for t in out["trades"][:3])
    assert out["trades"][3]["counts_as_loss"] is False and out["loss_streak"] == 3
    # outside the window → not active; disabled → not active
    assert _run(evaluate(db, "u1", _cfg(), NOW + timedelta(hours=5)))["active"] is False
    assert _run(evaluate(db, "u1", _cfg(anti_tilt_enabled=False), NOW))["active"] is False


def test_anti_tilt_ignores_stats_excluded_and_other_accounts():
    from anti_tilt import evaluate
    db = FakeDb()
    db.trades.rows = [_closed(-9.5, 10, stats_excluded=True), _closed(-9.6, 20), _closed(-9.7, 30),
                      {**_closed(-9.8, 40), "account_id": "a2"}]
    out = _run(evaluate(db, "u1", _cfg(), NOW))
    assert out["active"] is False and len(out["trades"]) == 2


def test_anti_tilt_sites_share_the_helper():
    for rel in ("bot_runner.py", "routes/bot_routes.py", "routes/diagnostic_routes.py"):
        src = open(os.path.join(BACKEND, rel)).read()
        assert "anti_tilt_evaluate(" in src, rel
        assert "or 0) < 0 for r in" not in src, rel
    assert open(os.path.join(BACKEND, "routes/bot_routes.py")).read().count("anti_tilt_evaluate(") >= 3
    ui = open(os.path.join(FRONTEND, "components", "CooldownPanel.jsx")).read()
    assert "anti_tilt_trades" in ui and "counts_as_loss" in ui


# ------------------------------------------------------------------------------------------------ #41 market intel
def test_market_intel_route_requires_auth_and_is_user_scoped():
    src = open(os.path.join(BACKEND, "routes", "market_intel_routes.py")).read()
    assert "Depends(get_current_user)" in src and 'user["id"]' in src
    assert 'prefix="/market-intel"' in src
    assert "market_intel_router" in open(os.path.join(BACKEND, "server.py")).read()


def _seed(db):
    db.bot_configs.rows = [{"_id": ObjectId(), "user_id": "u1", "account_id": None, "geo_risk_gate_enabled": False},
                           {"_id": ObjectId(), "user_id": "u2", "account_id": None, "geo_risk_gate_enabled": True}]
    db.news_ai_cache.rows = [{"_id": "XAUUSD", "net": 1.2, "label": "bullish", "headlines": 6, "fetched_at": (NOW - timedelta(minutes=5)).isoformat(),
                              "items": [{"title": "Fed signals pause", "source": "Reuters", "publishedAt": (NOW - timedelta(minutes=30)).isoformat(),
                                         "url": "https://x/1", "score": 1.5, "why": "dovish"},
                                        {"title": "old news", "source": "RSS", "publishedAt": (NOW - timedelta(hours=9)).isoformat(), "score": 2.0}]}]
    db.central_bank_releases.rows = [{"_id": "l1", "bank": "Fed", "ccy": "USD", "kind": "decision", "title": "FOMC statement", "link": "https://fed/1",
                                      "published": (NOW - timedelta(minutes=20)).strftime("%a, %d %b %Y %H:%M:%S GMT"), "seen_at": (NOW - timedelta(minutes=19)).isoformat()},
                                     {"_id": "l2", "bank": "ECB", "ccy": "EUR", "kind": "release", "title": "Speech", "link": "https://ecb/1",
                                      "published": (NOW - timedelta(days=3)).strftime("%a, %d %b %Y %H:%M:%S GMT"), "seen_at": (NOW - timedelta(days=3)).isoformat()}]
    db.dxy_cache.rows = [{"_id": "latest", "current_price": 102.3, "regime": "bullish_usd", "fetched_at": (NOW - timedelta(hours=60)).isoformat()}]
    db.tips_cache.rows = [{"key": "10Y_REAL", "real_yield_10y": 2.84, "delta_5d": -0.07, "real_yield_10y_date": "2026-10-14", "regime": "bullish_gold",
                           "fetched_at": (NOW - timedelta(hours=3)).isoformat(), "source": "fred:DFII10"}]
    db.fred_cache.rows = [{"_id": "snapshot", "payload": {"fetched_at": (NOW - timedelta(hours=2)).isoformat(), "series": {
        "VIXCLS": {"value": 16.9, "date": "2026-10-15", "vol_regime": "normal", "pct_20d": 40.0, "change_5d": -0.5},
        "GVZCLS": {"value": 22.0, "date": "2026-10-01", "vol_regime": "elevated"}}}}]
    db.risk_regime_advisory.rows = [{"_id": ObjectId(), "at": (NOW - timedelta(hours=1)).isoformat(), "geo_risk": 40.0, "risk_off_score": 31.0, "bucket": "low",
                                     "components": {"geo_risk": 40.0, "vix": 20.0, "gvz": 55.0, "cb_event": None}, "top_drivers": ["gvz", "geo_risk", "vix"],
                                     "missing": ["cb_event"], "regions": {"middle_east": {"score": 70}, "taiwan": {"score": 20}}, "top_regions": ["middle_east", "taiwan"]},
                                    {"_id": ObjectId(), "at": (NOW - timedelta(days=3)).isoformat(), "geo_risk": 20.0}]
    db.trade_decisions.rows = [{"_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "status": "rejected", "stage": "gate", "ts": (NOW - timedelta(hours=2)).isoformat(),
                                "reason": "Macro freeze: HIGH-impact USD CPI in 5min"},
                               {"_id": ObjectId(), "user_id": "u2", "symbol": "XAUUSD", "status": "rejected", "stage": "gate", "ts": NOW.isoformat(),
                                "reason": "Macro freeze: other user's row"}]
    db.platform_state.rows = [{"_id": "news_sources", "calendar": {"status": "OK", "active_source": "forexfactory", "last_fetch_at": (NOW - timedelta(minutes=10)).isoformat(),
                                                                   "sources": {"finnhub": {}}, "finnhub": {"configured": False}},
                               "rss_ai": {"last_fetch_at": (NOW - timedelta(minutes=10)).isoformat(), "last_scored_at": (NOW - timedelta(minutes=9)).isoformat(),
                                          "newsapi_items": 12, "headlines": 18, "finnhub_items": 0}}]


def _build(db, monkeypatch, user="u1", now=NOW):
    import market_intel
    import economic_calendar

    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz else now.replace(tzinfo=None)
    monkeypatch.setattr(market_intel, "datetime", _FixedDT)
    monkeypatch.setattr(market_intel.time, "time", lambda: now.timestamp())
    monkeypatch.setattr(economic_calendar.time, "time", lambda: now.timestamp())
    monkeypatch.setattr(economic_calendar, "get_events", _ev)
    monkeypatch.setattr("news_status.newsapi_configured", lambda: True)
    monkeypatch.setattr("providers.finnhub.configured", lambda: False)
    monkeypatch.setattr("news_status.load_source_status", lambda: _ret(db.platform_state.rows[0] if db.platform_state.rows else {}))
    return _run(market_intel.build(db, user))


async def _ret(v):
    return v


async def _ev():
    t = NOW.timestamp()
    return [{"title": "US CPI", "country": "USD", "impact": "high", "when_ts": t + 600, "when": "x", "forecast": "3.1%", "previous": "3.0%", "actual": None, "source": "forexfactory"},
            {"title": "ECB rate", "country": "EUR", "impact": "high", "when_ts": t - 1200, "when": "x", "forecast": "2.0%", "previous": "2.0%", "actual": "2.25%",
             "surprise": {"pct": 12.5}, "source": "finnhub"},
            {"title": "AUD jobs", "country": "AUD", "impact": "high", "when_ts": t + 300, "when": "x"},
            {"title": "US low", "country": "USD", "impact": "low", "when_ts": t + 900, "when": "x"}]


def test_market_intel_sections_staleness_shadow_and_scoping(monkeypatch):
    db = FakeDb()
    _seed(db)
    out = _build(db, monkeypatch)
    # 1 — next events: only high-impact USD/EUR/XAU currencies, released rows carry actual + surprise, freeze line present
    ev = out["next_events"]
    assert [e["currency"] for e in ev["events"]] == ["EUR", "USD"]
    assert ev["events"][0]["released"] is True and ev["events"][0]["actual"] == "2.25%" and ev["events"][0]["surprise"]["pct"] == 12.5
    assert ev["events"][1]["released"] is False and ev["events"][1]["actual"] is None
    assert ev["calendar_source"] == "forexfactory" and ev["freeze"] and "XAUUSD" in ev["freeze"]
    # 2 — headlines: last 4 h only, direction per instrument, bias gauge
    hl = out["headlines"]
    assert len(hl["headlines"]) == 1 and hl["headlines"][0]["direction"]["XAUUSD"] == "bullish" and hl["headlines"][0]["urgency"] == "high"
    assert hl["bias"]["XAUUSD"]["net"] == 1.2 and hl["bias"]["EURUSD"]["net"] is None
    # 3 — central banks: latest per bank, last-60-min highlight
    banks = {b["bank"]: b for b in out["central_banks"]["banks"]}
    assert banks["Fed"]["recent"] is True and banks["ECB"]["recent"] is False and banks["Fed"]["link"] == "https://fed/1"
    # 4 — macro gauges: DXY 60 h old → stale; GVZ 15 days old → stale; VIX fresh; real yield fresh
    m = out["macro"]
    assert m["dxy"]["stale"] is True and m["dxy"]["value"] == 102.3
    assert m["gvz"]["stale"] is True and m["vix"]["stale"] is False and m["vix"]["regime"] == "normal"
    assert m["real_yield_10y"]["value"] == 2.84 and m["real_yield_10y"]["stale"] is False
    # 5 — geo risk with top regions + sparkline (7 d)
    assert out["geo_risk"]["score"] == 40.0 and out["geo_risk"]["top_regions"][0] == "middle_east" and len(out["geo_risk"]["sparkline"]) == 2
    # 6 — SHADOW label while the flag is off (u1); u2 has the flag on → label must say what it does
    assert out["risk_regime"]["label"] == "SHADOW — not affecting trades" and out["risk_regime"]["score"] == 31.0
    assert out["risk_regime"]["gate_enabled"] is False
    out2 = _build(db, monkeypatch, user="u2")
    assert out2["risk_regime"]["gate_enabled"] is True and "no gate behaviour is implemented" in out2["risk_regime"]["label"]
    # 7 — usage lines: per instrument, last decision is USER-SCOPED (u1 sees its own row, not u2's)
    use = {u["instrument"]: u for u in out["usage"]}
    assert set(use) == {"EURUSD", "XAUUSD"}
    assert use["XAUUSD"]["last_decision"]["reason"].startswith("Macro freeze: HIGH-impact USD CPI")
    assert use["XAUUSD"]["macro_freeze"]["on"] is True and "gold_windows" in use["XAUUSD"] and "gold_windows" not in use["EURUSD"]
    assert use["XAUUSD"]["news_bias"]["net"] == 1.2 and use["XAUUSD"]["news_bias"]["gate"]["BUY"] == "pass"
    # 8 — source health: not-configured is grey info (not error); DXY stale
    sh = {r["feed"]: r for r in out["source_health"]}
    assert sh["Finnhub calendar"]["status"] == "not_configured" and sh["Finnhub news"]["status"] == "not_configured"
    assert sh["NewsAPI"]["status"] == "ok" and sh["DXY"]["status"] == "stale" and sh["Central-bank RSS"]["items_last_hour"] == 1
    # no secrets anywhere in the payload
    blob = json.dumps(out)
    assert not re.search(r'"(token|api_key|apikey|secret|password|authorization)[^"]*"\s*:', blob, re.I)
    assert "FINNHUB_API_KEY" in blob and "token" not in blob.lower().replace("not configured", "")


def test_market_intel_degrades_gracefully_with_empty_db(monkeypatch):
    out = _build(FakeDb(), monkeypatch)
    assert out["headlines"]["available"] is False and out["central_banks"]["available"] is False
    assert out["geo_risk"]["available"] is False and out["risk_regime"]["available"] is False
    assert out["risk_regime"]["label"] == "SHADOW — not affecting trades"
    assert out["macro"]["dxy"]["stale"] is True and out["macro"]["gvz"]["note"] == "not available"
    assert len(out["usage"]) == 2 and len(out["source_health"]) == 8


def test_market_intel_scrub_drops_credential_like_keys():
    from market_intel import scrub
    assert scrub({"a": 1, "token": "x", "nested": [{"api_key": "y", "ok": 2}]}) == {"a": 1, "nested": [{"ok": 2}]}


def test_market_intel_ui_wired_and_display_only():
    card = open(os.path.join(FRONTEND, "components", "MarketIntelCard.jsx")).read()
    assert '/market-intel' in card and "60000" in card and "DISPLAY ONLY" in card
    for tid in ("intel-next-events", "intel-headlines", "intel-central-banks", "intel-macro", "intel-geo", "intel-risk-regime", "intel-usage", "intel-sources", "intel-regime-label"):
        assert tid in card, tid
    assert "<MarketIntelCard />" in open(os.path.join(FRONTEND, "pages", "Agents.jsx")).read()
    assert "<MarketIntelCard compact />" in open(os.path.join(FRONTEND, "pages", "Dashboard.jsx")).read()
    assert "api.post" not in card and "api.put" not in card   # read-only


def test_market_intel_only_http_links_are_emitted():
    from market_intel import _safe_url, central_banks
    assert _safe_url("javascript:alert(1)") is None and _safe_url("data:text/html,x") is None
    assert _safe_url("https://fed/1") == "https://fed/1"
    out = central_banks([{"bank": "Fed", "ccy": "USD", "title": "x", "link": "javascript:alert(1)", "seen_at": NOW.isoformat()}], NOW)
    assert out["banks"][0]["link"] is None
