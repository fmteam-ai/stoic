"""v1.60.40 — calendar feed backup (second source, merge by time+currency, 2 h staleness alert, 6 h fail-safe),
loop heartbeats for ops_outbox / billing / deploy_watch, config-drift telemetry."""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _ev(title, country, impact, ts, source=None):
    e = {"title": title, "country": country, "impact": impact, "when_ts": ts,
         "when": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(), "forecast": "", "previous": ""}
    if source:
        e["source"] = source
    return e


async def _no_shared(*a, **k):
    return False


async def _nop_store(*a, **k):
    return None


def _reset(ec):
    for st in ec._sources.values():
        st.update({"events": None, "fetched_at": 0, "last_error": None})
    ec._cache.update({"events": None, "expires_at": 0, "fetched_at": 0, "last_error": None})


@pytest.fixture(autouse=True)
def _isolate_shared_cache(monkeypatch):
    """v1.60.43 — these tests exercise the fetch path, not the shared DB cache."""
    import economic_calendar as ec
    monkeypatch.setattr(ec, "_load_shared_cache", _no_shared)
    monkeypatch.setattr(ec, "_store_shared_cache", _nop_store)


def test_financecalendar_parse_and_country_mapping():
    import economic_calendar as ec
    payload = {"events": [
        {"name": "UK GDP", "time_utc": "2026-10-15T06:00:00+00:00", "impact": "high", "all_day": False, "prior": "0.4%"},
        {"name": "Retail Sales", "time_utc": "2026-10-15T12:30:00+00:00", "impact": "high", "all_day": False},
        {"name": "ECB decision", "time_utc": "2026-10-29T13:15:00+00:00", "impact": "high", "all_day": False},
        {"name": "Is the market open on X?", "time_utc": None, "impact": "low", "all_day": True},
        {"name": "Jobless claims", "time_utc": "2026-10-16T12:30:00Z", "impact": "weird", "all_day": False},
    ]}
    ev = ec.parse_financecalendar(payload)
    assert [(e["country"], e["impact"], e["title"]) for e in ev] == [
        ("GBP", "high", "UK GDP"), ("USD", "high", "Retail Sales"), ("EUR", "high", "ECB decision"), ("USD", "low", "Jobless claims")]
    assert all(e["source"] == "financecalendar" for e in ev)
    assert ec._fc_country("FOMC decision") == "USD" and ec._fc_country("Japan CPI") == "JPY" and ec._fc_country("China GDP") == "CNY"


def test_merge_by_time_and_currency_primary_wins():
    import economic_calendar as ec
    t = 1_800_000_000.0
    ff = [_ev("PPI m/m", "USD", "high", t), _ev("Core PPI m/m", "USD", "high", t), _ev("GDP m/m", "GBP", "high", t - 23400)]
    fc = [_ev("PPI", "USD", "high", t + 20, "financecalendar"),            # same minute + currency → dropped
          _ev("ECB decision", "EUR", "high", t + 2700, "financecalendar"),  # new → added
          _ev("Canada CPI", "CAD", "high", t, "financecalendar")]           # same minute, other currency → added
    out = ec.merge_events(ff, fc)
    assert [e["title"] for e in out] == ["GDP m/m", "PPI m/m", "Core PPI m/m", "Canada CPI", "ECB decision"]
    assert out[1]["source"] == "forexfactory" and out[-1]["source"] == "financecalendar"


def test_source_fallback_when_primary_fails(monkeypatch):
    import economic_calendar as ec
    _reset(ec)
    now = time.time()

    async def ff_fail():
        raise RuntimeError("429 Too Many Requests")

    async def fc_ok():
        return [_ev("Retail Sales", "USD", "high", now + 3600, "financecalendar")]
    monkeypatch.setattr(ec, "_fetch_events", ff_fail)
    monkeypatch.setattr(ec, "_fetch_financecalendar", fc_ok)

    async def nop():
        return None
    monkeypatch.setattr(ec, "_persist_status", nop)
    evs = _run(ec.get_events())
    assert [e["title"] for e in evs] == ["Retail Sales"]
    st = ec.feed_status()
    assert st["active_source"] == "financecalendar" and st["status"] == "DEGRADED" and st["failsafe_active"] is False
    assert st["sources"]["forexfactory"]["status"] == "DOWN" and "429" in st["sources"]["forexfactory"]["last_error"]
    assert st["sources"]["financecalendar"]["status"] == "OK" and st["stale"] is False
    assert ec._cache["expires_at"] - now > 3000          # one healthy source → normal 1 h cache
    # both fail → 10 min back-off, stale events kept
    monkeypatch.setattr(ec, "_fetch_financecalendar", ff_fail)
    ec._cache["expires_at"] = 0
    evs = _run(ec.get_events())
    assert [e["title"] for e in evs] == ["Retail Sales"] and ec._cache["expires_at"] - time.time() < 700
    assert "forexfactory" in ec._cache["last_error"] and "financecalendar" in ec._cache["last_error"]
    _reset(ec)


def test_stale_alert_after_2h_and_failsafe_after_6h():
    import economic_calendar as ec
    _reset(ec)
    now = time.time()
    ec._sources["forexfactory"].update({"events": [_ev("NFP", "USD", "high", now + 7200)], "fetched_at": now - 3 * 3600})
    ec._sources["financecalendar"].update({"events": [_ev("NFP", "USD", "high", now + 7200)], "fetched_at": now - 2.5 * 3600})
    st = ec.feed_status()
    assert st["stale"] is True and st["status"] == "STALE" and st["failsafe_active"] is False
    assert st["active_source"] == "financecalendar"            # freshest of the two stale sources
    # fresh secondary clears the alert
    ec._sources["financecalendar"]["fetched_at"] = now - 600
    assert ec.feed_status()["stale"] is False and ec.feed_status()["status"] == "OK"
    # both > 6 h → fail-safe: default US slots are added to the stale real prints
    ec._sources["forexfactory"]["fetched_at"] = now - 7 * 3600
    ec._sources["financecalendar"]["fetched_at"] = now - 6.5 * 3600
    assert ec.failsafe_active(now) and ec.feed_status()["status"] == "FAILSAFE"
    merged = ec._assemble(now)
    assert any(e["source"] == "failsafe" for e in merged) and any(e["title"] == "NFP" for e in merged)
    _reset(ec)


def test_failsafe_schedule_us_slots_and_fomc():
    import economic_calendar as ec
    # Wed 2026-10-28 is an FOMC statement day; Thu 29 is not; Sat 31 / Sun 1 skipped
    now = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc).timestamp()
    ev = ec.failsafe_events(now, days=4)
    by_day = {}
    for e in ev:
        by_day.setdefault(e["when"][:10], []).append(e["when"][11:16])
    assert by_day["2026-10-28"] == ["12:30", "14:00", "18:00"]
    assert by_day["2026-10-29"] == ["12:30", "14:00"] and by_day["2026-10-30"] == ["12:30", "14:00"]
    assert "2026-10-31" not in by_day and "2026-11-01" not in by_day
    assert all(e["country"] == "USD" and e["impact"] == "high" and e["source"] == "failsafe" for e in ev)
    # past slots (> 1 h ago) are not emitted
    late = datetime(2026, 10, 29, 15, 30, tzinfo=timezone.utc).timestamp()
    assert [e["when"][11:16] for e in ec.failsafe_events(late, days=0)] == []
    # the fail-safe windows actually freeze gold at 12:30
    wins = ec.freeze_windows("XAUUSD", ev)
    t1230 = datetime(2026, 10, 28, 12, 30, tzinfo=timezone.utc).timestamp()
    assert any(w["start_ts"] <= t1230 <= w["end_ts"] for w in wins)


def test_loops_record_progress_heartbeats():
    for path, loop_name in (("deploy_watch.py", '"deploy_watch.loop"'), ("background_loops.py", '"_billing_loop"'),
                            (os.path.join("routes", "panic_routes.py"), '"ops_outbox_loop"')):
        src = open(os.path.join(ROOT, path), encoding="utf-8").read()
        assert f"record_progress({loop_name}" in src, path


def test_config_drift_telemetry_endpoints_exist():
    bot = open(os.path.join(ROOT, "routes", "bot_routes.py"), encoding="utf-8").read()
    assert '@router.post("/config/drift")' in bot and "config_drift_events" in bot
    ops = open(os.path.join(ROOT, "routes", "ops_routes.py"), encoding="utf-8").read()
    assert '@router.get("/ops/config-drift")' in ops and "config_drift_events" in ops
    fe = os.path.join(ROOT, "..", "frontend", "src")
    assert 'api.post("/bot/config/drift"' in open(os.path.join(fe, "pages", "BotConfig.jsx"), encoding="utf-8").read()
    assert "<ConfigDriftCard />" in open(os.path.join(fe, "pages", "BotHealth.jsx"), encoding="utf-8").read()
    card = open(os.path.join(fe, "components", "NewsSourcesCard.jsx"), encoding="utf-8").read()
    assert "FAIL-SAFE" in card and "STALE" in card and "calendar-attribution" in card
