"""v1.60.39 — deferred shadow replay on M1 bars (fallback M15) + one idea per decision cluster.

15 Oct: 211/211 deferred scalps came back sl_first on bar 1 — M15 bars with 6–9 pt stops make "both touched in one
bar → SL first" decide everything. M1 bars (EA feed or scalp ticks) resolve the order; ambiguous bars are flagged;
re-evaluations of the same idea (symbol + direction within 15 min) are counted once."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
T0 = datetime(2026, 10, 15, 13, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _bar(t, o, h, l, c):  # noqa: E741
    return {"t": int(t), "o": o, "h": h, "l": l, "c": c}


def _dec(action, ts, entry, sl, tp, status="deferred", symbol="XAUUSD"):
    return {"_id": ObjectId(), "user_id": "u1", "status": status, "symbol": symbol, "ts": ts.isoformat(),
            "signal": {"action": action, "entry_price": entry, "stop_loss": sl, "take_profit": tp, "scope": "hf_scalp"}}


def test_replay_flags_ambiguous_bar_and_honours_horizon():
    from ablation import HORIZON_BARS, replay_outcome
    # SELL 4080 / SL 4087 / TP 4066 — one M15 bar spanning both → sl_first but AMBIGUOUS
    oc = replay_outcome("SELL", 4080, 4087, 4066, [_bar(1, 4080, 4090, 4060, 4070)])
    assert oc["outcome"] == "sl_first" and oc["ambiguous"] is True and oc["bars"] == 1
    # clean TP → not ambiguous
    oc = replay_outcome("SELL", 4080, 4087, 4066, [_bar(1, 4080, 4082, 4065, 4066)])
    assert oc["outcome"] == "tp_first" and oc["ambiguous"] is False
    # horizon: 1440 M1 bars walk the same 24 h as 96 M15 bars
    bars = [_bar(i, 4080, 4081, 4079, 4080) for i in range(1500)] + [_bar(1500, 4080, 4081, 4060, 4066)]
    assert replay_outcome("SELL", 4080, 4087, 4066, bars, horizon_bars=1440)["outcome"] == "timeout"
    assert replay_outcome("SELL", 4080, 4087, 4066, bars, horizon_bars=1600)["outcome"] == "tp_first"
    assert replay_outcome("SELL", 4080, 4087, 4066, bars)["bars"] == HORIZON_BARS


def test_m1_bars_from_ticks_aggregates_mid_price_per_minute():
    from deferred_shadow import m1_bars_from_ticks
    e = T0.timestamp()
    ticks = [{"tm": int((e + 10) * 1000), "b": 4079.9, "a": 4080.1},
             {"tm": int((e + 30) * 1000), "b": 4081.9, "a": 4082.1},
             {"tm": int((e + 50) * 1000), "b": 4078.9, "a": 4079.1},
             {"tm": int((e + 70) * 1000), "b": 4065.9, "a": 4066.1},
             {"tm": int((e - 5) * 1000), "b": 4000.0, "a": 4000.2},      # before the decision — excluded
             {"junk": True}]
    bars = m1_bars_from_ticks(ticks, e, e + 3600)
    assert [b["t"] for b in bars] == [int(e), int(e) + 60]
    assert bars[0] == {"t": int(e), "o": 4080.0, "h": 4082.0, "l": 4079.0, "c": 4079.0, "n": 3}
    assert bars[1]["c"] == 4066.0 and bars[1]["n"] == 1


def test_m1_replay_resolves_what_m15_called_sl_first():
    """The 15 Oct pattern: the M15 bar touches both SL and TP; the M1 tape shows TP was hit first."""
    from deferred_shadow import resolve_deferred_outcomes
    db = FakeDb()
    e = T0.timestamp()
    db.trade_decisions.rows.append(_dec("SELL", T0, 4080.0, 4087.0, 4066.0))
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15",
                                     "bars": [_bar(e + 900, 4080, 4090, 4060, 4070)]})
    # scalp ticks: price walks down to TP in minutes 1–3, only later spikes through SL
    ticks = []
    for i, px in enumerate([4079.0, 4075.0, 4070.0, 4066.0, 4065.5, 4075.0, 4088.0]):
        ticks.append({"tm": int((e + 60 * (i + 1) + 5) * 1000), "b": px - 0.1, "a": px + 0.1})
    db.scalp_ticks.rows.append({"user_id": "u1", "symbol": "XAUUSD.fx", "first_ms": ticks[0]["tm"],
                                "last_ms": ticks[-1]["tm"], "ticks": ticks})
    out = _run(resolve_deferred_outcomes(db, "u1"))
    assert out["resolved"] == 1 and out["by_timeframe"] == {"M1": 1}
    sh = db.trade_decisions.rows[0]["shadow"]
    assert sh["outcome"] == "tp_first" and sh["timeframe"] == "M1" and sh["ambiguous"] is False and sh["r"] == 2.0


def test_m1_feed_from_ea_is_preferred_over_ticks():
    from deferred_shadow import m1_bars_after
    db = FakeDb()
    e = T0.timestamp()
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M1", "label_version": 2,
                                     "bars": [_bar(e - 60, 1, 1, 1, 1), _bar(e + 60, 2, 2, 2, 2), _bar(e + 90000, 3, 3, 3, 3)]})
    db.scalp_ticks.rows.append({"user_id": "u1", "symbol": "XAUUSD", "first_ms": int((e + 1) * 1000),
                                "last_ms": int((e + 2) * 1000), "ticks": [{"tm": int((e + 1) * 1000), "b": 9, "a": 9}]})
    bars = _run(m1_bars_after(db, "u1", "XAUUSD", e))
    assert [b["o"] for b in bars] == [2]            # only bars inside (epoch, epoch+24h]


def test_falls_back_to_m15_when_no_m1_coverage_and_when_m1_runs_out():
    from deferred_shadow import resolve_deferred_outcomes
    db = FakeDb()
    e = T0.timestamp()
    # (a) no M1 at all → M15 replay, flagged ambiguous when both touch
    db.trade_decisions.rows.append(_dec("SELL", T0, 4080.0, 4087.0, 4066.0))
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15",
                                     "bars": [_bar(e + 900, 4080, 4090, 4060, 4070)]})
    out = _run(resolve_deferred_outcomes(db, "u1"))
    sh = db.trade_decisions.rows[0]["shadow"]
    assert out["by_timeframe"] == {"M15": 1} and sh["timeframe"] == "M15" and sh["ambiguous"] is True
    # (b) a few M1 ticks with no verdict, then the M15 tape finishes the walk (clean TP two bars later)
    t1 = T0 + timedelta(hours=2)
    e1 = t1.timestamp()
    db.trade_decisions.rows.append(_dec("BUY", t1, 4080.0, 4073.0, 4094.0))
    db.scalp_ticks.rows.append({"user_id": "u1", "symbol": "XAUUSD", "first_ms": int((e1 + 60) * 1000),
                                "last_ms": int((e1 + 120) * 1000),
                                "ticks": [{"tm": int((e1 + 60) * 1000), "b": 4080.0, "a": 4080.2},
                                          {"tm": int((e1 + 120) * 1000), "b": 4081.0, "a": 4081.2}]})
    db.intraday_candles.rows[0]["bars"] += [_bar(e1 + 900, 4080, 4083, 4079, 4082), _bar(e1 + 1800, 4082, 4095, 4081, 4094)]
    out = _run(resolve_deferred_outcomes(db, "u1"))
    sh = db.trade_decisions.rows[1]["shadow"]
    assert out["resolved"] == 1 and sh["timeframe"] == "M15" and sh["outcome"] == "tp_first" and sh["r"] == 2.0


def test_legacy_m15_shadows_are_rescored_on_m1_or_tagged():
    from deferred_shadow import rescore_legacy_shadows, sweep_deferred_shadows
    db = FakeDb()
    e = T0.timestamp()
    legacy = _dec("SELL", T0, 4080.0, 4087.0, 4066.0)
    legacy["shadow"] = {"outcome": "sl_first", "r": -1.0, "bars": 1, "utc_band": "New York (13:00–21:00 UTC)"}
    other = _dec("SELL", T0 + timedelta(hours=3), 1.1, 1.2, 1.0, symbol="EURUSD")
    other["shadow"] = {"outcome": "sl_first", "r": -1.0, "bars": 1, "utc_band": "New York (13:00–21:00 UTC)"}
    db.trade_decisions.rows += [legacy, other]
    ticks = [{"tm": int((e + 60 * (i + 1)) * 1000), "b": px - 0.1, "a": px + 0.1}
             for i, px in enumerate([4079.0, 4072.0, 4066.0, 4065.0, 4070.0, 4088.0])]
    db.scalp_ticks.rows.append({"user_id": "u1", "symbol": "XAUUSD", "first_ms": ticks[0]["tm"],
                                "last_ms": ticks[-1]["tm"], "ticks": ticks})
    out = _run(rescore_legacy_shadows(db, "u1"))
    assert out == {"rescored": 1, "tagged_m15": 1}
    sh = db.trade_decisions.rows[0]["shadow"]
    assert sh["outcome"] == "tp_first" and sh["timeframe"] == "M1" and sh["rescored_from"]["outcome"] == "sl_first"
    assert db.trade_decisions.rows[1]["shadow"]["timeframe"] == "M15"
    assert _run(rescore_legacy_shadows(db, "u1")) == {"rescored": 0, "tagged_m15": 0}   # idempotent
    assert "rescore_legacy_shadows" in open(os.path.join(os.path.dirname(__file__), "..", "..", "deferred_shadow.py"),
                                           encoding="utf-8").read().split("async def sweep_deferred_shadows")[1]
    assert _run(sweep_deferred_shadows(db)) == 0


def test_cluster_deferred_counts_one_idea_per_symbol_direction_within_15_min():
    from deferred_shadow import cluster_deferred
    from strategy_report import build_report
    base = T0
    rows = [_dec("SELL", base, 4080, 4087, 4066),                              # idea 1
            _dec("SELL", base + timedelta(minutes=5), 4079, 4086, 4065),       # re-eval of idea 1
            _dec("SELL", base + timedelta(minutes=14), 4079, 4086, 4065),      # re-eval of idea 1 (within 15 of the FIRST)
            _dec("SELL", base + timedelta(minutes=16), 4078, 4085, 4064),      # idea 2 (window measured from cluster start)
            _dec("BUY", base + timedelta(minutes=6), 4080, 4073, 4094),        # idea 3 — other direction
            _dec("SELL", base + timedelta(minutes=7), 1.1, 1.2, 1.0, symbol="EURUSD")]  # idea 4 — other symbol
    rows[0]["shadow"] = {}                                                      # first unresolved …
    rows[1]["shadow"] = {"outcome": "tp_first", "r": 2.0, "utc_band": "New York (13:00–21:00 UTC)", "timeframe": "M1", "label_version": 2}
    rows[2]["shadow"] = {"outcome": "sl_first", "r": -1.0, "utc_band": "New York (13:00–21:00 UTC)", "timeframe": "M15"}
    rows[3]["shadow"] = {"outcome": "sl_first", "r": -1.0, "utc_band": "New York (13:00–21:00 UTC)", "timeframe": "M15",
                         "ambiguous": True}
    ideas = cluster_deferred(rows)
    assert len(ideas) == 4
    first = ideas[0]
    assert first["re_evaluations"] == 3 and first["shadow"]["outcome"] == "tp_first"   # … takes the earliest resolved shadow
    sell2 = next(i for i in ideas if i["ts"] == rows[3]["ts"])
    assert sell2["re_evaluations"] == 1 and sell2["signal"]["action"] == "SELL"

    rep = build_report([], 30, rows)
    assert rep["deferred"] == 6 and rep["deferred_ideas"] == 4
    ny = next(r for r in rep["engines"][0]["deferred_shadow"] if r["band"].startswith("New York"))
    # v1.60.40 — only the M1 verdict counts; the M15 (ambiguous) one is reported as awaiting_m1, never in the win rate
    assert ny["n"] == 4 and ny["re_evaluations"] == 6 and ny["labelled"] == 1 and ny["invalid"] == 1   # v1.60.49: untagged M15 = pre-v2 → invalid
    assert ny["tp_first"] == 1 and ny["win_rate"] == 100.0 and ny["ambiguous"] == 0
    fe = open(os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "src", "components",
                           "StrategyReport.jsx"), encoding="utf-8").read()
    assert "re_evaluations" in fe and "awaiting_m1" in fe and "ambiguous" in fe
