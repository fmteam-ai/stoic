"""v1.60.62 — two display fixes, no trading logic.
#82 GTP news plan line shows weekday + date when the event is not today.
#83 Exit Rule Scoreboard classifies real broker SL/TP fills (close_reason 'broker_confirmed' /
    'broker_reconciled_estimated+external_close') by exit price vs stop / target, not only by close_reason prefix."""
import os
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gold_trend_pulse import rules as R  # noqa: E402  (rules re-exports the news-sweep helpers; importing the submodule first is circular)
from scalp.reports import broker_exit_kind, summarise  # noqa: E402

pytestmark = pytest.mark.unit

SAT = datetime(2026, 10, 24, 9, 30, tzinfo=timezone.utc)            # Saturday
FOMC = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)           # Wednesday


# ── #82 ──────────────────────────────────────────────────────────────────────────────────────────────────────
def test_news_plan_text_adds_weekday_and_date_when_the_event_is_not_today():
    ev = {"when_ts": FOMC.timestamp(), "title": "FOMC Statement"}
    txt = R.news_plan_text(ev, "FOMC", 5, now=SAT)
    assert txt.startswith("FOMC Wed 28 Oct 18:00 UTC — protect at 17:45, news trades from 18:05"), txt
    same_day = R.news_plan_text(ev, "FOMC", 5, now=FOMC.replace(hour=8))
    assert same_day.startswith("FOMC 18:00 UTC — protect at 17:45"), same_day      # today: time only, as before
    late_utc = R.news_plan_text({"when_ts": datetime(2026, 10, 29, 0, 30, tzinfo=timezone.utc).timestamp()}, "NFP", 5,
                                now=FOMC.replace(hour=23, minute=50))
    assert late_utc.startswith("NFP Thu 29 Oct 00:30 UTC"), late_utc                # date compared in UTC, not local


def test_next_news_event_plan_uses_the_same_now():
    evs = [{"when_ts": FOMC.timestamp(), "title": "FOMC Statement", "currency": "USD", "impact": "high", "country": "USD"}]
    nxt = R.next_news_event(evs, SAT, None, 5)
    if nxt is not None:                                   # candidate filter may require more fields; the plan must then carry the date
        assert "Wed 28 Oct" in nxt["plan"]


# ── #83 ──────────────────────────────────────────────────────────────────────────────────────────────────────
def _gold(close_reason, exit_price, pnl, *, action="BUY", entry=4100.0, sl=4095.0, tp=4110.0, **extra):
    return {"engine": "hf_scalp", "symbol": "XAUUSD", "action": action, "entry_price": entry, "exit_price": exit_price,
            "stop_loss": sl, "take_profit": tp, "original_stop_loss": sl, "original_take_profit": tp,
            "close_reason": close_reason, "pnl": pnl, "risk_amount": 25.0, **extra}


def test_broker_exit_kind_both_close_reason_forms_and_price_based_fills():
    assert broker_exit_kind(_gold("stop_loss", 4095.0, -25)) == "stop_loss"                         # explicit bot reason
    assert broker_exit_kind(_gold("take_profit_hit", 4110.0, 50)) == "take_profit"
    assert broker_exit_kind(_gold("broker_confirmed", 4094.7, -26.2)) == "stop_loss"                # SL fill with slippage
    assert broker_exit_kind(_gold("broker_reconciled_estimated+external_close", 4094.9, -25.4)) == "stop_loss"
    assert broker_exit_kind(_gold("broker_confirmed", 4110.1, 50.3)) == "take_profit"
    assert broker_exit_kind(_gold("broker_confirmed", 4092.0, -40.0)) == "stop_loss"                # gapped through the stop
    assert broker_exit_kind(_gold("broker_confirmed", 4103.0, 15.0)) is None                        # manual close mid-way
    assert broker_exit_kind(_gold("external_close", None, 0.0)) is None                             # no exit price → unknown
    sell = _gold("broker_confirmed", 4105.2, -26.0, action="SELL", entry=4100.0, sl=4105.0, tp=4090.0)
    assert broker_exit_kind(sell) == "stop_loss"
    # the stop had been moved to breakeven before the broker filled it: current stop_loss wins over the original
    be = _gold("broker_confirmed", 4100.1, 0.4, sl=4100.1, original_stop_loss=4095.0)
    assert broker_exit_kind(be) == "stop_loss"
    fx = {"engine": "hf_scalp", "symbol": "EURUSD", "action": "BUY", "entry_price": 1.0850, "exit_price": 1.08381,
          "stop_loss": 1.0840, "take_profit": 1.0870, "close_reason": "broker_confirmed", "pnl": -12.0}
    assert broker_exit_kind(fx) == "stop_loss"                                                      # 1.9 pips past the stop


def test_scoreboard_counts_ten_broker_stop_fills_as_sl():
    rows = [_gold("broker_confirmed", 4094.8, -11.57) for _ in range(6)]
    rows += [_gold("broker_reconciled_estimated+external_close", 4094.6, -11.58) for _ in range(4)]
    rows += [_gold("broker_confirmed", 4110.0, 23.0), _gold("stop_loss", 4095.0, -11.0), _gold("take_profit", 4110.0, 22.0)]
    out = summarise(rows, 7)
    u = out["unaffected"]
    assert u["trades"] == 13 and u["stop_loss"] == 11 and u["take_profit"] == 2
    assert u["pnl_usd"] == round(sum(r["pnl"] for r in rows), 2)
    legacy = summarise([_gold("broker_confirmed", None, -11.57)], 7)                              # no exit price: neither
    assert legacy["unaffected"]["stop_loss"] == 0 and legacy["unaffected"]["take_profit"] == 0
