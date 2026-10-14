"""v1.60.36 — scalp exits:
1 trend-ride skips SCALP_ENGINES (a scalp keeps TP = 2×SL)
2 trend-ride / vol-retarget keep tp1/tp2/tp3 in sync with the new take_profit / tp_pips
3 fill re-base: SL moved to fill ± sl_pips when |fill − signal| > 2 pips (never loosens)
4 slippage measured against the signal price as well as the requested price
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


# ── 1 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_trend_ride_is_skipped_for_scalp_engines():
    src = _read("backend", "bot_runner.py")
    i = src.index("trend_ride_check(signal[\"action\"], _feats, _base)")
    guard = src[i - 400:i]
    assert 'cfg.get("trend_ride_enabled", True) and _eng_of(signal) not in _ride_exempt' in guard
    assert "SCALP_ENGINES as _ride_exempt" in src and "engine_for_signal as _eng_of" in src
    from strategy_engines import SCALP_ENGINES, engine_for_signal
    # a real bot scalp row: engine None, scope hf_scalp_fast → still exempt
    assert engine_for_signal({"engine": None, "scope": "hf_scalp_fast"}) in SCALP_ENGINES
    assert engine_for_signal({"engine": "breakout_m15"}) not in SCALP_ENGINES


# ── 2 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_trend_ride_widens_take_profit_and_tp_fields_together():
    from exit_sync import widen_signal_targets
    # SELL gold: entry 4080, SL 69.5 pips, single TP 2×SL = 139 pips → tp1 = tp2 = tp3 = take_profit
    sig = {"entry_price": 4080.0, "action": "SELL", "take_profit": 4066.1, "tp1": 4066.1, "tp2": 4066.1, "tp3": 4066.1,
           "tp_pips": [139.0, 139.0, 139.0]}
    assert widen_signal_targets(sig, 1.8, "XAUUSD") is True
    # 139 × 1.8 = 250.2 → capped at 200 (tp3 cap) for take_profit, 100 for tp1 (tier caps)
    assert sig["take_profit"] == 4060.0 and sig["tp3"] == 4060.0 and sig["tp2"] == 4060.0 and sig["tp1"] == 4070.0
    assert sig["tp_pips"] == [100.0, 200.0, 200.0]
    assert sig["tp3"] == sig["take_profit"]                     # the broker TP and the trade's tp3 agree
    # BUY, uncapped: everything scales by the multiplier
    sig = {"entry_price": 1.1000, "action": "BUY", "take_profit": 1.1030, "tp1": 1.1010, "tp2": 1.1020, "tp3": 1.1030,
           "tp_pips": [10, 20, 30]}
    widen_signal_targets(sig, 1.5, "EURUSD")
    assert sig["take_profit"] == 1.1045 and sig["tp1"] == 1.1015 and sig["tp2"] == 1.103 and sig["tp3"] == 1.1045
    assert sig["tp_pips"] == [15.0, 30.0, 45.0]
    assert widen_signal_targets({"entry_price": 1.0, "take_profit": 1.1}, 1.0, "EURUSD") is False


def test_vol_retarget_rescales_tp_fields_with_the_pip_ladder():
    import adaptive_exits as ae
    from exit_sync import tp_fields_from_pips
    assert tp_fields_from_pips(4000.0, "SELL", "XAUUSD", [100, 200, 200]) == {"tp1": 3990.0, "tp2": 3980.0, "tp3": 3980.0}
    assert tp_fields_from_pips(1.1, "BUY", "EURUSD", [10, 20]) == {"tp1": 1.101, "tp2": 1.102}
    db = FakeDb()
    t = {"_id": ObjectId(), "user_id": "u1", "symbol": "XAUUSD", "action": "BUY", "entry_price": 4000.0, "stop_loss": 3990.0,
         "sl_pips": 100.0, "tp_pips": [100.0, 200.0, 200.0], "tp1": 4010.0, "tp2": 4020.0, "tp3": 4020.0, "status": "open",
         "atr15_at_entry": 6.0}
    db.trades.rows.append(dict(t))
    import time
    db.intraday_candles.rows.append({"user_id": "u1", "symbol": "XAUUSD", "timeframe": "M15", "bars": [{"t": time.time()}]})
    act = {"kind": "EXIT_RETARGET_VOL", "new_tp_pips": [160.0, 320.0, 320.0], "atr_ratio": 1.6, "scale": 1.6}
    with patch.object(ae, "vol_retarget", lambda *a, **k: act), patch.object(ae, "compute_features", lambda bars: {"atr15": 9.6}), \
            patch("ws_manager.manager") as ws, patch("trade_events.append", AsyncMock()):
        ws.broadcast = AsyncMock()
        assert asyncio.new_event_loop().run_until_complete(
            ae.manage_exits(db, db.trades.rows[0], {}, current=4005.0, pips_up=50.0)) is True
    row = db.trades.rows[0]
    assert row["tp_pips"] == [160.0, 320.0, 320.0]
    assert row["tp1"] == 4016.0 and row["tp2"] == 4032.0 and row["tp3"] == 4032.0


# ── 3 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def _sell_gold():
    # signal 4080.0, SL 69.5 pips → 4086.95 ; fill 5.5 pips WORSE for a SELL = 4079.45
    return {"_id": ObjectId(), "symbol": "XAUUSD", "action": "SELL", "entry_price": 4080.0, "stop_loss": 4086.95,
            "sl_pips": 69.5, "status": "open"}


def test_fill_rebase_moves_the_stop_to_fill_plus_sl_pips_when_gap_exceeds_2_pips():
    from exit_sync import fill_rebase
    rb = fill_rebase(_sell_gold(), 4079.45)
    assert rb and rb["gap_pips"] == 5.5 and rb["new_sl"] == 4086.4 and rb["previous_sl"] == 4086.95
    # fill-to-SL distance is the designed 69.5 pips again (was 75.0)
    assert round((rb["new_sl"] - 4079.45) / 0.1, 1) == 69.5
    buy = {**_sell_gold(), "action": "BUY", "stop_loss": 4073.05}          # BUY filled 6 pips worse = 4080.6
    rb = fill_rebase(buy, 4080.6)
    assert rb["new_sl"] == 4073.65 and round((4080.6 - rb["new_sl"]) / 0.1, 1) == 69.5


def test_fill_rebase_is_skipped_for_small_gaps_and_never_loosens():
    from exit_sync import fill_rebase
    assert fill_rebase(_sell_gold(), 4079.85) is None                    # 1.5 pips → leave it
    assert fill_rebase(_sell_gold(), 4080.0) is None                     # exact fill
    assert fill_rebase(_sell_gold(), 4080.6) is None                     # FAVOURABLE fill → rebased SL would be looser
    assert fill_rebase({**_sell_gold(), "sl_pips": 0}, 4079.0) is None   # no designed distance → nothing to re-base


def test_bridge_report_wires_rebase_as_a_fenced_modify_sl():
    br = _read("backend", "routes", "bridge_routes.py")
    i = br.index("rb = fill_rebase(trade, actual)")
    block = br[i:i + 900]
    assert 'if rb and not trade.get("pending_modification") and not trade.get("close_requested"):' in block
    assert '"type": "MODIFY_SL", "new_sl": rb["new_sl"], "reason": "fill_rebase"' in block
    assert 'update["fill_rebase"] = {**rb' in block


# ── 4 ──────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_slippage_is_measured_against_the_signal_price_too():
    from exit_sync import signal_slippage
    assert signal_slippage("SELL", 4080.0, 4079.45, "XAUUSD") == (5.5, 5.5)      # 14 Oct: 5–6 pips worse
    assert signal_slippage("BUY", 4080.0, 4080.6, "XAUUSD") == (6.0, 6.0)
    assert signal_slippage("SELL", 4080.0, 4080.6, "XAUUSD") == (0.0, -6.0)      # favourable: no slippage, negative gap
    br = _read("backend", "routes", "bridge_routes.py")
    assert 'update["signal_slippage_pips"], update["signal_gap_pips"] = signal_slippage(action, intended, actual, symbol)' in br
    assert 'update["slippage_pips"] = round(slip_pips, 2)' in br                   # the requested-price metric is kept
