"""v1.60.39 C8–C10 — economic calendar freeze refinements.

C8  gold is frozen only by configured currencies' HIGH-impact prints (default USD/EUR/GBP/JPY/CNY) — 15 Oct UK GDP
    froze gold 05:45–06:10 because "ALL" matched every country.
C9  USD medium-impact prints: ±5 min freeze, only when not already inside a HIGH window.
C10 macro avalanche: ≥2 USD HIGH prints within 5 min → after-window 10 → 20 min; "next freeze" exposed."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit

T0 = 1_800_000_000.0   # arbitrary epoch anchor


def _ev(title, country, impact, ts):
    return {"title": title, "country": country, "impact": impact, "when_ts": ts, "when": "", "forecast": "", "previous": ""}


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_c8_gold_frozen_only_by_configured_currencies(monkeypatch):
    import economic_calendar as ec
    monkeypatch.delenv("MACRO_FREEZE_BEFORE_MIN", raising=False)
    monkeypatch.delenv("MACRO_FREEZE_AFTER_MIN", raising=False)
    uk_gdp = _ev("GDP m/m", "GBP", "high", T0)
    cad_cpi = _ev("CPI m/m", "CAD", "high", T0)
    assert "ALL" not in ec.SYMBOL_CURRENCIES["XAUUSD"]
    # default: GBP freezes gold, CAD does not
    assert ec.relevant_events_for("XAUUSD", [uk_gdp, cad_cpi]) == [uk_gdp]
    w = ec.freeze_windows("XAUUSD", [uk_gdp, cad_cpi])
    assert len(w) == 1 and w[0]["event"] is uk_gdp and w[0]["kind"] == "high"
    assert w[0]["start_ts"] == T0 - 15 * 60 and w[0]["end_ts"] == T0 + 10 * 60
    # per-account: USD-only → UK GDP no longer freezes gold
    assert ec.freeze_windows("XAUUSD", [uk_gdp, cad_cpi], {"gold_freeze_currencies": ["USD"]}) == []
    # junk config falls back to the default list
    assert ec.freeze_settings({"gold_freeze_currencies": []})["gold_freeze_currencies"] == sorted(ec.GOLD_FREEZE_CURRENCIES_DEFAULT)
    # crypto keeps the "ALL" behaviour
    assert ec.relevant_events_for("BTCUSD", [uk_gdp, cad_cpi]) == [uk_gdp, cad_cpi]


def test_c8_macro_freeze_check_uses_account_cfg(monkeypatch):
    import economic_calendar as ec
    uk_gdp = _ev("GDP m/m", "GBP", "high", T0 + 120)

    async def events():
        return [uk_gdp]
    monkeypatch.setattr(ec, "get_events", events)
    monkeypatch.setattr(ec.time, "time", lambda: T0)
    assert _run(ec.macro_freeze_check("XAUUSD"))["frozen"] is True
    out = _run(ec.macro_freeze_check("XAUUSD", {"gold_freeze_currencies": ["USD", "EUR"]}))
    assert out["frozen"] is False and out["next_freeze"] is None


def test_c9_usd_medium_impact_short_freeze_outside_high_windows(monkeypatch):
    import economic_calendar as ec
    monkeypatch.delenv("MACRO_FREEZE_BEFORE_MIN", raising=False)
    monkeypatch.delenv("MACRO_FREEZE_AFTER_MIN", raising=False)
    claims = _ev("Unemployment Claims", "USD", "medium", T0)
    w = ec.freeze_windows("XAUUSD", [claims])
    assert len(w) == 1 and w[0]["kind"] == "medium"
    assert w[0]["start_ts"] == T0 - 5 * 60 and w[0]["end_ts"] == T0 + 5 * 60
    # inside a HIGH window (CPI at T0 too) → no separate medium window
    cpi = _ev("CPI m/m", "USD", "high", T0)
    kinds = [x["kind"] for x in ec.freeze_windows("XAUUSD", [claims, cpi])]
    assert kinds == ["high"]
    # medium freeze is per-account configurable
    assert ec.freeze_windows("XAUUSD", [claims], {"macro_freeze_medium_usd_enabled": False}) == []
    w = ec.freeze_windows("XAUUSD", [claims], {"macro_freeze_medium_min": 3})
    assert w[0]["start_ts"] == T0 - 180 and w[0]["end_ts"] == T0 + 180
    # non-USD medium prints do not freeze
    assert ec.freeze_windows("XAUUSD", [_ev("Retail Sales", "GBP", "medium", T0)]) == []
    # upcoming_for still lists medium prints; freeze reason names the short window
    assert "USD medium-impact" in ec._window_reason(ec.freeze_windows("XAUUSD", [claims])[0], T0 + 60)


def test_c10_macro_avalanche_extends_after_window(monkeypatch):
    import economic_calendar as ec
    monkeypatch.delenv("MACRO_FREEZE_BEFORE_MIN", raising=False)
    monkeypatch.delenv("MACRO_FREEZE_AFTER_MIN", raising=False)
    # 15 Oct 12:30 — PPI, Core PPI, Retail Sales, Core Retail Sales together
    cluster = [_ev(t, "USD", "high", T0) for t in ("PPI m/m", "Core PPI m/m", "Retail Sales m/m", "Core Retail Sales m/m")]
    w = ec.freeze_windows("XAUUSD", cluster)
    assert all(x["kind"] == "avalanche" and x["cluster_size"] == 4 for x in w)
    assert all(x["end_ts"] == T0 + 20 * 60 and x["start_ts"] == T0 - 15 * 60 for x in w)
    # a lone USD HIGH print keeps the 10-min after window
    solo = ec.freeze_windows("XAUUSD", [_ev("NFP", "USD", "high", T0)])
    assert solo[0]["kind"] == "high" and solo[0]["end_ts"] == T0 + 10 * 60
    # two prints 6 min apart are not a cluster; 4 min apart are
    assert {x["kind"] for x in ec.freeze_windows("XAUUSD", [_ev("a", "USD", "high", T0), _ev("b", "USD", "high", T0 + 360)])} == {"high"}
    assert {x["kind"] for x in ec.freeze_windows("XAUUSD", [_ev("a", "USD", "high", T0), _ev("b", "USD", "high", T0 + 240)])} == {"avalanche"}
    # EUR + USD HIGH together is not a USD avalanche
    assert {x["kind"] for x in ec.freeze_windows("XAUUSD", [_ev("a", "USD", "high", T0), _ev("b", "EUR", "high", T0)])} == {"high"}
    # configurable after-window
    w = ec.freeze_windows("XAUUSD", cluster, {"macro_freeze_avalanche_after_min": 30})
    assert w[0]["end_ts"] == T0 + 30 * 60
    # frozen 15 min after the cluster (would be clear under the plain 10-min rule)
    assert any(x["start_ts"] <= T0 + 15 * 60 <= x["end_ts"] for x in w)
    assert "MACRO AVALANCHE" in ec._window_reason(w[0], T0 + 15 * 60)


def test_c10_next_freeze_and_route_shape(monkeypatch):
    import economic_calendar as ec
    monkeypatch.delenv("MACRO_FREEZE_BEFORE_MIN", raising=False)
    monkeypatch.delenv("MACRO_FREEZE_AFTER_MIN", raising=False)
    past = _ev("old", "USD", "high", T0 - 3600)
    soon = _ev("Unemployment Claims", "USD", "medium", T0 + 1800)
    later = _ev("FOMC", "USD", "high", T0 + 7200)
    nf = ec.next_freeze("XAUUSD", [later, past, soon], now=T0)
    assert nf["title"] == "Unemployment Claims" and nf["freeze_kind"] == "medium"
    assert nf["freeze_start"].startswith(ec.datetime.fromtimestamp(T0 + 1800 - 300, tz=ec.timezone.utc).isoformat()[:16])

    async def events():
        return [later, past, soon]
    monkeypatch.setattr(ec, "get_events", events)
    monkeypatch.setattr(ec.time, "time", lambda: T0)
    out = _run(ec.macro_freeze_check("XAUUSD"))
    assert out["frozen"] is False and out["next_freeze"]["title"] == "Unemployment Claims"
    # a frozen response still carries the following window
    monkeypatch.setattr(ec.time, "time", lambda: T0 + 1800)
    out = _run(ec.macro_freeze_check("XAUUSD"))
    assert out["frozen"] is True and out["kind"] == "medium" and out["next_freeze"]["title"] == "FOMC"


def test_c8_c10_config_plumbing():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    models = open(os.path.join(root, "models.py"), encoding="utf-8").read()
    for f in ("macro_freeze_before_min: int = 15", "macro_freeze_after_min: int = 10",
              "macro_freeze_medium_usd_enabled: bool = True", "macro_freeze_medium_min: int = 5",
              "macro_freeze_avalanche_after_min: int = 20", "gold_freeze_currencies: List[str]"):
        assert f in models, f
    ai = open(os.path.join(root, "ai_signals.py"), encoding="utf-8").read()
    assert "macro_freeze_check(symbol, freeze_cfg)" in ai
    assert "freeze_cfg=cfg" in open(os.path.join(root, "agents", "strategy_agent.py"), encoding="utf-8").read()
    cal = open(os.path.join(root, "routes", "calendar_routes.py"), encoding="utf-8").read()
    assert '@router.get("/next-freeze/{symbol}")' in cal and "user_freeze_cfg" in cal
    fe = os.path.join(root, "..", "frontend", "src")
    assert "next-freeze-line" in open(os.path.join(fe, "pages", "Dashboard.jsx"), encoding="utf-8").read()
    bc = open(os.path.join(fe, "pages", "BotConfig.jsx"), encoding="utf-8").read()
    for tid in ("macro-freeze-config", "gold-freeze-currencies", "macro_freeze_avalanche_after_min", "macro_freeze_medium_usd_enabled"):
        assert tid in bc, tid
