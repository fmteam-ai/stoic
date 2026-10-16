"""v1.60.49 — deferred-shadow label integrity (#33 HOLD replayed as SELL, #34 broker-time ticks, #35 relabel script
in the image with --all, #36 invalid labels flagged, never scored)."""
import asyncio
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(BACKEND)
BROKER_OFF = 10800


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _bars(start, n, price):
    return [{"t": start + 60 * i, "o": price, "h": price + 0.5, "l": price - 0.5, "c": price} for i in range(n)]


# ----------------------------------------------------------------------------------------------- #33
def test_replay_outcome_refuses_hold():
    from ablation import replay_outcome
    bars = _bars(0, 10, 4000.0)
    assert replay_outcome("HOLD", 4000.0, 3995.0, 4010.0, bars) is None
    assert replay_outcome("", 4000.0, 3995.0, 4010.0, bars) is None
    assert replay_outcome("buy", 4000.0, 3995.0, 4010.0, bars)["outcome"] == "timeout"


def test_deferred_direction_prefers_stored_action_then_geometry():
    from deferred_shadow import deferred_direction
    assert deferred_direction({"action": "HOLD", "deferred_action": "BUY"}) == "BUY"
    assert deferred_direction({"action": "HOLD", "entry_price": 4000, "take_profit": 4010, "stop_loss": 3995}) == "BUY"
    assert deferred_direction({"action": "HOLD", "entry_price": 4000, "take_profit": 3990, "stop_loss": 4005}) == "SELL"
    assert deferred_direction({"action": "SELL"}) == "SELL"
    assert deferred_direction({"action": "HOLD"}) == "HOLD"


def test_orchestrator_keeps_pre_defer_direction_and_snapshot_stores_it():
    src = open(os.path.join(BACKEND, "agents", "orchestrator.py")).read()
    assert src.count('signal["deferred_action"] = signal.get("action")') == 2
    from trade_decisions import SNAPSHOT_FIELDS, _snapshot
    assert "deferred_action" in SNAPSHOT_FIELDS
    assert _snapshot({"action": "HOLD", "deferred_action": "BUY"})["deferred_action"] == "BUY"


# ----------------------------------------------------------------------------------------------- #34
def test_m1_bars_from_ticks_subtracts_broker_offset():
    from deferred_shadow import m1_bars_from_ticks
    utc_t = 1_760_000_000                       # a UTC minute boundary-ish epoch
    ticks = [{"tm": (utc_t + 30 + BROKER_OFF) * 1000, "b": 4000.0, "a": 4000.2}]   # EA stamps broker time
    assert m1_bars_from_ticks(ticks, utc_t - 1, utc_t + 3600) == [], "treated as UTC the tick lands 3 h later — outside"
    bars = m1_bars_from_ticks(ticks, utc_t - 1, utc_t + 3600, BROKER_OFF)
    assert len(bars) == 1 and bars[0]["t"] == (utc_t // 60) * 60


def test_m1_bars_after_uses_each_accounts_offset_and_widens_the_query():
    import deferred_shadow as ds
    db = FakeDb()
    acc = ObjectId()
    db.accounts.rows.append({"_id": acc, "user_id": "u1", "broker_time_info": {"server_gmt_offset_sec": 10799}})
    epoch = 1_760_000_000.0
    db.scalp_ticks.rows.append({"user_id": "u1", "account_id": str(acc), "symbol": "XAUUSD.r",
                                "first_ms": int((epoch + 60 + BROKER_OFF) * 1000), "last_ms": int((epoch + 120 + BROKER_OFF) * 1000),
                                "ticks": [{"tm": int((epoch + 90 + BROKER_OFF) * 1000), "b": 4001.0, "a": 4001.2}]})
    bars = _run(ds.m1_bars_after(db, "u1", "XAUUSD", epoch))
    assert len(bars) == 1 and bars[0]["t"] == int((epoch + 90) // 60) * 60 and abs(bars[0]["c"] - 4001.1) < 1e-9


# ----------------------------------------------------------------------------------------------- resolve / rescore
def _seed(db, *, action="HOLD", deferred_action=None, shadow=None):
    epoch = 1_760_000_000
    sig = {"action": action, "entry_price": 4000.0, "stop_loss": 3995.0, "take_profit": 4010.0, "symbol": "XAUUSD"}
    if deferred_action:
        sig["deferred_action"] = deferred_action
    row = {"_id": ObjectId(), "user_id": "u1", "status": "deferred", "symbol": "XAUUSD",
           "ts": datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(), "signal": sig}
    if shadow:
        row["shadow"] = shadow
    db.trade_decisions.rows.append(row)
    # M1 feed: price rallies to TP on bar 3 (a BUY wins; a SELL replay would have "hit SL")
    bars = _bars(epoch + 60, 1500, 4000.0)
    bars[2]["h"] = 4011.0
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M1", "bars": bars})
    return row


def test_resolve_labels_hold_rows_with_their_true_direction_and_stamps_version():
    import deferred_shadow as ds
    db = FakeDb()
    _seed(db, action="HOLD", deferred_action="BUY")
    out = _run(ds.resolve_deferred_outcomes(db, "u1"))
    sh = db.trade_decisions.rows[0]["shadow"]
    assert out["resolved"] == 1 and sh["outcome"] == "tp_first" and sh["direction"] == "BUY"
    assert sh["label_version"] == ds.LABEL_VERSION == 2 and sh["timeframe"] == "M1"


def test_rescore_fixes_v1_label_and_keeps_old_one_in_rescored_from():
    import deferred_shadow as ds
    db = FakeDb()
    _seed(db, action="HOLD", shadow={"outcome": "sl_first", "r": -1.0, "bars": 1, "timeframe": "M1", "utc_band": "London (06:00–13:00 UTC)"})
    out = _run(ds.rescore_legacy_shadows(db, "u1"))
    sh = db.trade_decisions.rows[0]["shadow"]
    assert out["rescored"] == 1 and sh["outcome"] == "tp_first" and sh["direction"] == "BUY"
    assert sh["rescored_from"] == {"timeframe": "M1", "outcome": "sl_first", "r": -1.0, "label_version": 1, "direction": None}
    assert sh["utc_band"] == "London (06:00–13:00 UTC)"
    # second default pass: nothing left below the current version
    assert _run(ds.rescore_legacy_shadows(db, "u1"))["visited"] == 0
    # --all re-scores it again (idempotent verdict)
    assert _run(ds.rescore_legacy_shadows(db, "u1", rescore_all=True))["rescored"] == 1
    assert db.trade_decisions.rows[0]["shadow"]["outcome"] == "tp_first"


def test_rescore_without_m1_coverage_marks_label_invalid_instead_of_trusting_it():
    import deferred_shadow as ds
    db = FakeDb()
    _seed(db, action="HOLD", shadow={"outcome": "sl_first", "r": -1.0, "bars": 1})
    db.intraday_candles.rows.clear()
    out = _run(ds.rescore_legacy_shadows(db, "u1"))
    sh = db.trade_decisions.rows[0]["shadow"]
    assert out["tagged_m15"] == 1 and sh["invalid"] is True and sh["label_version"] == 2 and sh["outcome"] == "sl_first"


# ----------------------------------------------------------------------------------------------- #36 reporting
def test_strategy_report_never_scores_invalid_or_v1_labels():
    from strategy_report import deferred_rows
    ts = "2026-10-15T08:00:00+00:00"
    sig = {"action": "HOLD", "entry_price": 4000.0, "take_profit": 4010.0, "stop_loss": 3995.0}
    rows = deferred_rows([
        {"ts": ts, "symbol": "XAUUSD", "signal": sig, "shadow": {"outcome": "sl_first", "timeframe": "M1"}},               # v1
        {"ts": "2026-10-15T09:00:00+00:00", "symbol": "XAUUSD", "signal": sig,
         "shadow": {"outcome": "sl_first", "timeframe": "M1", "label_version": 2, "invalid": True}},
        {"ts": "2026-10-15T10:00:00+00:00", "symbol": "XAUUSD", "signal": sig,
         "shadow": {"outcome": "tp_first", "r": 2.0, "timeframe": "M1", "label_version": 2}},
    ])
    r = rows[0]
    assert r["band"].startswith("London") and r["n"] == 3 and r["invalid"] == 2 and r["labelled"] == 1
    assert r["win_rate"] == 100.0
    page = open(os.path.join(ROOT, "frontend", "src", "components", "StrategyReport.jsx")).read()
    assert "-invalid-flag" in page and "INVALID" in page


# ----------------------------------------------------------------------------------------------- #35 script / image
def test_relabel_script_ships_in_image_and_supports_all():
    docker = open(os.path.join(ROOT, "Dockerfile.backend")).read()
    assert "scripts/relabel_deferred_shadows.py" in docker
    src = open(os.path.join(ROOT, "scripts", "relabel_deferred_shadows.py")).read()
    assert '"--all"' in src and "rescore_all=rescore_all" in src and "/app/backend" in src
