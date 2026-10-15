"""v1.60.40 — Strategy Report shows only M1-based deferred verdicts; one-off relabel script for the pre-M1 rows."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def test_only_m1_verdicts_enter_the_win_rate():
    from strategy_report import deferred_rows
    band = "London (06:00–13:00 UTC)"
    rows = [
        {"ts": "2026-10-15T07:00:00+00:00", "symbol": "XAUUSD", "signal": {"action": "SELL"},
         "shadow": {"outcome": "tp_first", "r": 2.0, "utc_band": band, "timeframe": "M1"}},
        {"ts": "2026-10-15T08:00:00+00:00", "symbol": "XAUUSD", "signal": {"action": "SELL"},
         "shadow": {"outcome": "sl_first", "r": -1.0, "utc_band": band, "timeframe": "M15", "ambiguous": True}},
        {"ts": "2026-10-15T09:00:00+00:00", "symbol": "XAUUSD", "signal": {"action": "SELL"},
         "shadow": {"outcome": "sl_first", "r": -1.0, "utc_band": band}},                  # legacy, untagged
        {"ts": "2026-10-15T10:00:00+00:00", "symbol": "XAUUSD", "signal": {"action": "SELL"}},   # unresolved
    ]
    r = next(x for x in deferred_rows(rows) if x["band"] == band)
    assert r["n"] == 4 and r["labelled"] == 1 and r["awaiting_m1"] == 2
    assert r["win_rate"] == 100.0 and r["sl_first"] == 0 and r["ambiguous"] == 0


def test_relabel_script_exists_and_is_idempotent_by_construction():
    src = open(os.path.join(ROOT, "scripts", "relabel_deferred_shadows.py"), encoding="utf-8").read()
    assert "rescore_legacy_shadows" in src and '"shadow.timeframe": {"$exists": False}' in src
    assert "--dry-run" in src and "trade_decisions" in src
    assert "bot_configs" not in src and "risk" not in src.lower().replace("no trading or risk state", "")
    fe = open(os.path.join(ROOT, "frontend", "src", "components", "StrategyReport.jsx"), encoding="utf-8").read()
    assert "M1 VERDICTS ONLY" in fe and "awaiting_m1" in fe
