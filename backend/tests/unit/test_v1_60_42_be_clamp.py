"""v1.60.42 — breakeven clamp on fast spikes (OnEquity #46719148): the EA clamps a MODIFY_SL to the broker's stops
level measured from the current price; a breakeven requested while price has snapped back to within the stops level of
entry was clamped BELOW entry and the stop then closed the trade at −0.92. The manager now skips (and logs
`be_clamped`) instead of sending a below-entry stop, and retries once the price clears the stops level."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ACC = ObjectId()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _account(stops_level_points=300, point=0.01, fresh=True):
    return {"_id": ACC, "user_id": "u1", "broker_server": "OnEquity-Demo",
            "symbol_specs_updated_at": (datetime.now(timezone.utc) - timedelta(seconds=5 if fresh else 2 * 86400)).isoformat(),
            "symbol_specs": {"XAUUSD": {"point": point, "stops_level_points": stops_level_points, "freeze_level_points": 0}}}


def _trade(**kw):
    t = {"_id": ObjectId(), "user_id": "u1", "account_id": str(ACC), "symbol": "XAUUSD", "action": "BUY", "engine": None,
         "scope": "hf_scalp_fast", "entry_price": 4000.0, "stop_loss": 3996.0, "sl_pips": 400, "tp_pips": [100, 200, 200],
         "lot_size": 0.05, "original_lot_size": 0.05, "mt5_ticket": 46719148, "status": "open", "mode": "demo", "spread_pips": 2.5,
         "opened_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}
    t.update(kw)
    return t


# ── pure clamp maths ─────────────────────────────────────────────────────────────────────────────────────
def test_be_clamp_detects_a_stop_the_broker_would_push_below_entry():
    from trade_manager import be_clamp
    buy = {"entry_price": 4000.0, "action": "BUY"}
    # stops level 300 points = 3.00; price spiked back to 4001.0 → the EA clamps SL to ≤ 3998.0 → below entry
    c = be_clamp(buy, 4000.25, 4001.0, 3.0)
    assert c and c["clamped_to"] == 3998.0 and c["requested_sl"] == 4000.25 and c["entry"] == 4000.0
    assert be_clamp(buy, 4000.25, 4005.0, 3.0) is None                 # price well clear: 4002.0 ≥ entry → fine
    assert be_clamp(buy, 4000.25, 4003.5, 3.0) is None                 # clamped to 4000.5, still on the profitable side
    sell = {"entry_price": 4000.0, "action": "SELL"}
    c = be_clamp(sell, 3999.75, 3999.0, 3.0)
    assert c and c["clamped_to"] == 4002.0                             # above entry on a SELL = losing side
    assert be_clamp(sell, 3999.75, 3995.0, 3.0) is None
    assert be_clamp(buy, 4000.25, 4001.0, 0) is None                   # no stops level known → no prediction
    assert be_clamp(buy, 4000.25, 0, 3.0) is None and be_clamp(buy, 4000.25, 4001.0, None) is None


# ── scalp +0.5R breakeven ────────────────────────────────────────────────────────────────────────────────
def _manage(db, t, cfg, price):
    import trade_manager as tm
    quote = AsyncMock(return_value={"price": price})
    with patch.object(tm, "get_db", lambda: db), patch.object(tm, "get_quote", quote), patch.object(tm, "ws_manager") as ws, \
            patch("adaptive_exits.manage_exits", AsyncMock()):
        ws.broadcast = AsyncMock()
        _run(tm._manage_one_trade(t, cfg))
        return ws.broadcast


def test_scalp_breakeven_is_skipped_and_logged_when_the_clamp_would_land_below_entry():
    """+0.5R was reached (BE armed by the manager at the next cycle) but the quote has spiked back to entry+1.0 with a
    3.0 stops level — the stop the EA could place is ≤ 3998.0."""
    db = FakeDb()
    db.accounts.rows.append(_account())
    t = _trade()
    db.trades.rows.append(t)
    # pips_up uses the quote: entry 4000 + 0.1 * 250 pips = +25 → ≥ 0.5R (200 pips); make the quote 4001.0 instead
    # by stating a smaller R: sl_pips 10 → 0.5R = 5 pips, quote +1.0 = +10 pips
    t["sl_pips"] = 10
    bc = _manage(db, t, {}, 4001.0)
    row = db.trades.rows[0]
    assert row.get("pending_modification") is None and row.get("be_target_sl") is None   # nothing sent to the EA
    assert row["be_clamped_count"] == 1
    c = row["be_clamped"][0]
    assert c["source"] == "scalp_breakeven_0_5r" and c["decision"] == "skip" and c["clamped_to"] == 3998.0 < 4000.0
    assert c["requested_sl"] > 4000.0                                                     # entry + costs was asked
    assert any(call.args[2]["action"] == "BE_CLAMPED" for call in bc.call_args_list)
    # same clamp a few seconds later: evidence is not duplicated
    _manage(db, row, {}, 4001.0)
    assert db.trades.rows[0]["be_clamped_count"] == 1 and len(db.trades.rows[0]["be_clamped"]) == 1


def test_scalp_breakeven_goes_out_once_the_price_clears_the_stops_level():
    db = FakeDb()
    db.accounts.rows.append(_account())
    t = _trade(sl_pips=10)
    db.trades.rows.append(t)
    _manage(db, t, {}, 4001.0)                                                            # clamped → skipped
    assert db.trades.rows[0].get("pending_modification") is None
    _manage(db, db.trades.rows[0], {}, 4004.0)                                            # clamp floor 4001.0 ≥ entry → send
    pm = db.trades.rows[0]["pending_modification"]
    assert pm["type"] == "MODIFY_SL" and pm["reason"] == "scalp_breakeven_0_5r" and pm["new_sl"] > 4000.0
    assert db.trades.rows[0]["be_target_sl"] == pm["new_sl"]


def test_without_fresh_symbol_specs_the_request_goes_out_as_before():
    db = FakeDb()
    db.accounts.rows.append(_account(fresh=False))                                       # stale specs → no prediction
    t = _trade(sl_pips=10)
    db.trades.rows.append(t)
    _manage(db, t, {}, 4001.0)
    assert db.trades.rows[0]["pending_modification"]["type"] == "MODIFY_SL" and "be_clamped" not in db.trades.rows[0]


# ── legacy TP1 breakeven ─────────────────────────────────────────────────────────────────────────────────
def test_tp1_be_only_path_skips_and_retries_next_cycle():
    db = FakeDb()
    db.accounts.rows.append(_account())
    t = _trade(scope="breakout_m15", tp_pips=[10, 200, 200])
    db.trades.rows.append(t)
    _manage(db, t, {"let_winners_run": True}, 4001.0)                                    # +10 pips = TP1, clamp → skip
    row = db.trades.rows[0]
    assert not row.get("tp1_closed") and row.get("pending_modification") is None and row["be_clamped"][0]["source"] == "tp1_let_winners_run"
    _manage(db, row, {"let_winners_run": True}, 4004.0)
    row = db.trades.rows[0]
    assert row["tp1_closed"] is True and row["pending_modification"]["new_sl"] == 4000.0 == row["be_target_sl"]


def test_tp1_partial_still_banks_and_the_be_is_retried_after_the_clamp():
    db = FakeDb()
    db.accounts.rows.append(_account())
    t = _trade(scope="breakout_m15", tp_pips=[10, 200, 200])
    db.trades.rows.append(t)
    _manage(db, t, {}, 4001.0)
    row = db.trades.rows[0]
    pm = row["pending_modification"]
    assert pm["type"] == "PARTIAL_CLOSE" and "new_sl" not in pm and row["tp1_target_lot"] == 0.03   # half banked, no below-entry stop
    assert row["be_clamped"][0]["source"] == "tp1_partial_close" and row.get("be_target_sl") is None
    row["pending_modification"], row["tp1_closed"] = None, True                           # EA acked the partial
    bc = _manage(db, row, {}, 4004.0)
    row = db.trades.rows[0]
    assert row["pending_modification"]["type"] == "MODIFY_SL" and row["pending_modification"]["reason"] == "tp1_be_retry"
    assert row["be_target_sl"] == 4000.0
    assert any(call.args[2]["action"] == "BE_RETRY_AFTER_CLAMP" for call in bc.call_args_list)


def test_scalp_engine_partial_combo_drops_a_clamped_be():
    src = open(os.path.join(ROOT, "backend", "scalp", "engine.py"), encoding="utf-8").read()
    i = src.index("from trade_manager import be_clamp")
    assert '"source": "scalp_partial_be_combo"' in src[i:i + 1200] and "be_sl = None" in src[i:i + 1200]
    assert src.index("from trade_manager import be_clamp") < src.index("be_sl = round_to_tick(be_sl, tick)")
