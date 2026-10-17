"""v1.60.64 #86 — gold spread missing on broker-renamed symbols + spread-history sampling.

Live evidence (17 Oct 17:32 UTC): current_spreads had NO gold key on OnEquity (GOLD#), STARTRADER (XAUUSD+) and
VTMarkets (XAUUSD.crp) — EA ≤ 1.66 called SymbolInfoTick on the raw TrackedSymbols names and skipped them silently.
EA 1.67 resolves the broker name and always adds the chart symbol; the server folds broker names to their base."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

import entry_costs as EC  # noqa: E402
import trade_costs as TC  # noqa: E402
from pip_utils import pip_size  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SRC = open(os.path.join(ROOT, "static", "EmergentTradingBridge.mq5"), encoding="utf-8").read()


def _run(c):
    return asyncio.get_event_loop().run_until_complete(c)


def _body(name: str) -> str:
    i = SRC.index("\n" + name)
    return SRC[i:SRC.index("\n}\n", i) + 3]


# ── A. EA 1.67 ─────────────────────────────────────────────────────────────────────────────────────────────
def test_ea_reports_spreads_under_the_broker_symbol_and_the_chart_symbol():
    assert '#property version   "1.67"' in SRC and '#define EA_CLIENT_VERSION "1.67"' in SRC
    build = _body("string BuildSpreadsJson(")
    assert "ResolveBrokerSymbol(sym)" in build and "AppendSpread(out, _Symbol, done)" in build
    assert "_spread_syms_at" in build and "> 300" in build            # re-resolved at most every 5 min, not per heartbeat
    append = _body("string AppendSpread(")
    assert "SymbolPipSize(sym)" in append and 'StringFind(done, "|" + sym + "|") >= 0' in append   # no duplicate keys
    resolve = _body("string ResolveBrokerSymbol(")
    assert 'base = "GOLD"' in resolve and 'base = "SILVER"' in resolve and "SymbolHasBase(us, ubase, strict)" in resolve
    assert "pass < 4" in resolve and "bool strict = (pass % 2 == 0);" in resolve      # strict before relaxed, base before alias
    assert 'return "";' in resolve                                     # not found → nothing sent (never a wrong symbol)
    # the heartbeat itself is untouched (frozen since 1.64): it still sends "spreads": BuildSpreadsJson()
    assert "string spreads = BuildSpreadsJson();" in _body("void SendHeartbeat(")


def _ea_has_base(u: str, base: str, strict: bool) -> bool:   # Python mirror of the EA's SymbolHasBase (resolver)
    if not u.startswith(base):
        return False
    if len(u) == len(base):
        return True
    c = u[len(base)]
    if "0" <= c <= "9":
        return False
    return not (strict and "A" <= c <= "Z")


def _ea_pip(sym: str) -> float:                        # Python mirror of the EA's SymbolPipSize (prefix, like base_symbol)
    u = sym.upper()
    if u.startswith(("XAUUSD", "GOLD")):
        return 0.10
    if u.startswith(("XAGUSD", "SILVER")):
        return 0.01
    if u.startswith("BTCUSD"):
        return 1.00
    if u.startswith("ETHUSD"):
        return 0.10
    if u.startswith(("US30", "NAS100", "GER40", "UK100")):
        return 1.00
    if u.startswith("SPX500"):
        return 0.10
    if "JPY" in u:
        return 0.01
    return 0.0001


def test_ea_pip_table_mirrors_the_server_for_broker_names():
    pip_fn = _body("double SymbolPipSize(")
    for b in ('"XAUUSD"', '"GOLD"', '"XAGUSD"', '"SILVER"', '"BTCUSD"', '"US30"', '"SPX500"'):
        assert b in pip_fn
    assert "SymbolStartsWith(u, " in pip_fn                              # prefix match, exactly like pip_utils.base_symbol
    for name in ("XAUUSD", "XAUUSD+", "XAUUSD.crp", "GOLD#", "XAUUSDm", "GOLD", "XAGUSD.r", "BTCUSD", "US30.cash",
                 "SPX500", "EURUSD.r", "GBPJPY#", "USDJPY"):
        assert _ea_pip(name) == pytest.approx(pip_size(name)), name
    assert _ea_has_base("GOLDEUR", "GOLD", True) is False and _ea_has_base("GOLD#", "GOLD", True) is True
    assert _ea_has_base("XAUUSDM", "XAUUSD", True) is False and _ea_has_base("XAUUSDM", "XAUUSD", False) is True
    assert _ea_has_base("US300", "US30", False) is False and _ea_has_base("XAUUSD.CRP", "XAUUSD", True) is True


# ── B. server folds broker names to the base ─────────────────────────────────────────────────────────────────
def test_normalize_spread_keys_adds_the_base_and_never_overwrites_it():
    assert EC.normalize_spread_keys({"GOLD#": 3.3, "BTCUSD": 85.0}) == {"GOLD#": 3.3, "XAUUSD": 3.3, "BTCUSD": 85.0}
    assert EC.normalize_spread_keys({"xauusd.crp": 2.9})["XAUUSD"] == 2.9
    both = EC.normalize_spread_keys({"XAUUSD": 0.7, "XAUUSD+": 9.9})
    assert both["XAUUSD"] == 0.7 and both["XAUUSD+"] == 9.9
    assert EC.spreads_by_base({"XAUUSD+": 2.4, "BTCUSD": 40.0}) == {"XAUUSD": 2.4, "BTCUSD": 40.0}
    assert EC.spreads_by_base({"XAUUSD+": 9.9, "XAUUSD": 0.7}) == {"XAUUSD": 0.7}       # the exact base key wins
    assert EC.normalize_spread_keys(None) == {} and EC.spreads_by_base({}) == {}
    acc = {"current_spreads": EC.normalize_spread_keys({"GOLD#": 3.3})}
    assert EC.spread_at_entry(acc, "XAUUSD") == 3.3 and EC.spread_at_entry(acc, "GOLD#") == 3.3


def test_heartbeat_stores_base_keys_and_samples_once_per_base():
    src = open(os.path.join(ROOT, "routes", "bridge_routes.py"), encoding="utf-8").read()
    hb = src.split("async def heartbeat(")[1].split("\nasync def ")[0]
    assert "per_base = spreads_by_base(clean)" in hb and "clean = normalize_spread_keys(clean)" in hb
    assert hb.index("clean = normalize_spread_keys(clean)") < hb.index('set_doc["current_spreads"] = clean')
    assert "record_spread(str(acc[\"_id\"]), per_base)" in hb and "record_spread_samples(db, str(acc[\"_id\"]), per_base)" in hb
    from ea_capabilities import capabilities_for
    assert "spreads_resolved_v1" in capabilities_for("1.67") and "spreads_resolved_v1" not in capabilities_for("1.66")


# ── B4. one sample per minute → the 60 cover the whole hour ─────────────────────────────────────────────────
def test_spread_history_keeps_one_sample_per_minute_across_the_whole_hour():
    db = FakeDb()
    start = datetime(2026, 10, 20, 14, 0, 0, tzinfo=timezone.utc)
    for i in range(720):                                                   # a heartbeat every 5 s for one hour
        _run(TC.record_spread_samples(db, "a1", {"XAUUSD+": 2.0 + (i // 12) * 0.01, "XAUUSD": 2.0 + (i // 12) * 0.01},
                                      start + timedelta(seconds=5 * i)))
    assert len(db.spread_history.rows) == 1                                # broker + base name → ONE doc, one sample
    mins = db.spread_history.rows[0]["minutes"]["14"]
    assert len(mins) == 60 and sorted(mins) == [f"{m:02d}" for m in range(60)]
    assert mins["00"] == pytest.approx(2.0) and mins["59"] == pytest.approx(2.59)   # first AND last minute present
    st = TC.hour_stats_from_docs(db.spread_history.rows, 14)
    assert st["n"] == 60 and st["median"] == pytest.approx(2.295, abs=0.01)


def test_legacy_pushed_hour_lists_are_still_read():
    legacy = {"hours": {"9": [2.0, 2.2, 2.4]}}
    mixed = {"hours": {"9": [2.0]}, "minutes": {"9": {"00": 3.0, "01": 3.2}}}
    assert TC.hour_stats_from_docs([legacy], 9)["n"] == 3
    assert TC.hour_stats_from_docs([mixed], 9) == {"n": 3, "median": 3.0, "p90": 3.2}


# ── B5. Bot Health: a traded symbol without a spread is visible ──────────────────────────────────────────────
def test_spread_coverage_flags_missing_gold_while_the_market_is_open():
    tue = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
    fresh = tue.isoformat()
    accs = [
        {"_id": "a1", "label": "OnEquity", "ea_version": "1.66", "current_spreads": {"BTCUSD": 78.7}, "spreads_updated_at": fresh},
        {"_id": "a2", "label": "RoboForex", "ea_version": "1.66", "current_spreads": {"XAUUSD": 0.7}, "spreads_updated_at": fresh},
        {"_id": "a3", "label": "VTMarkets", "ea_version": "1.67", "current_spreads": {"XAUUSD.CRP": 2.9, "XAUUSD": 2.9},
         "spreads_updated_at": fresh},
        {"_id": "a4", "label": "STARTRADER", "ea_version": "1.67", "current_spreads": {"XAUUSD+": 2.1},
         "spreads_updated_at": (tue - timedelta(minutes=30)).isoformat()},
    ]
    cfgs = [{"account_id": a, "symbols": ["XAUUSD"]} for a in ("a1", "a2", "a3", "a4")]
    gaps = EC.spread_coverage(accs, cfgs, tue)
    assert [(g["label"], g["missing"], g["stale"]) for g in gaps] == [("OnEquity", ["XAUUSD"], False), ("STARTRADER", ["XAUUSD"], True)]
    sat = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    assert EC.spread_coverage(accs, cfgs, sat) == []                       # gold closed → nothing to report
    diag = open(os.path.join(ROOT, "routes", "diagnostic_routes.py"), encoding="utf-8").read()
    assert '"Spread reported for traded symbols"' in diag and "spreads_resolved_v1" in diag
