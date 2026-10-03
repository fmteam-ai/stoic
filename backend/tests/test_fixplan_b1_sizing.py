"""Fix plan step B1 — position sizing (R1, R9, R10, B10, B11).

R1   JPY-cross / USD-less cross notional is the BASE-currency amount in USD, not `units × price`.
R9   every scale-down floors to the broker step and SKIPS below the minimum (never back up to 0.01).
R10  pip value is price-aware (USDCHF/USDCAD/USDJPY from price, crosses via quote→USD);
     the broker's volume_min / volume_step from the heartbeat symbol_specs are honoured.
B10  authority reduction floors (and refuses below minimum) instead of rounding up.
B11  paper P&L goes through pips × USD-per-pip × lots; a paper trade settles exactly once.
"""
import os
import sys
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId
from pymongo import MongoClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import importlib.util as _ilu
_spec = _ilu.spec_from_file_location(
    "_tests_root_conftest",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "conftest.py"))
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_arun = _mod.run_async

TAG = "_test_fixplan_b1"


# ------------------------------------------------------------------ R1
def test_r1_jpy_cross_notional_is_base_currency_in_usd():
    from instruments import notional_usd
    eurjpy = notional_usd("EURJPY", 1.0, 165.0)
    assert 95_000 <= eurjpy <= 125_000, eurjpy          # was 16,500,000 (≈150× too high)
    assert notional_usd("GBPJPY.fx", 0.5, 190.0) == pytest.approx(0.5 * 100_000 * 1.27)
    assert notional_usd("EURGBP", 1.0, 0.85) == pytest.approx(108_000)
    # exact paths unchanged
    assert notional_usd("EURUSD", 1.0, 1.08) == pytest.approx(108_000)
    assert notional_usd("USDJPY", 1.0, 150.0) == pytest.approx(100_000)
    assert notional_usd("XAUUSD", 1.0, 4000.0) == pytest.approx(400_000)


# ------------------------------------------------------------------ R10
def test_r10_pip_value_is_price_aware():
    from pip_utils import pip_value_usd_per_lot as pv
    assert pv("EURUSD") == 10.0
    assert pv("USDCHF", price=0.80) == pytest.approx(12.5)        # 10 / price
    assert pv("USDCAD", price=1.36) == pytest.approx(10 / 1.36)
    assert pv("USDJPY", price=150.0) == pytest.approx(1000 / 150)
    assert pv("EURJPY") == 6.5                                     # fixed approximation without a rate
    assert pv("EURJPY", quote_usd=1 / 160) == pytest.approx(6.25)   # live JPY rate wins
    assert pv("EURGBP") == pytest.approx(12.7)                     # 10 GBP → USD
    assert pv("USDCHF") == pytest.approx(11.2)                     # no price → CHF table approximation, never 0
    assert pv("XAUUSD", price=4000) == 10.0
    assert pv("EURUSD", "microcent") == pytest.approx(0.01)


def test_r10_usdchf_lot_about_20pct_smaller_for_same_risk():
    from risk import compute_lot_for_account
    acc = {"equity": 10_000, "account_type": "standard"}
    profile = {"risk_pct": 1.0, "tp_atr_mult": 2, "sl_atr_mult": 1, "min_confidence": 60, "kelly_cap": 0}
    out = compute_lot_for_account(acc, "USDCHF", 0.80, 0.79, 70, profile, kelly_enabled=False)
    # $100 risk / (100 pips × $12.5) = 0.08 (was 0.10 with the flat $10 pip)
    assert out["sizing_valid"] and out["lot_size"] == pytest.approx(0.08)


def test_r10_broker_volume_step_and_min_from_symbol_specs():
    from pip_utils import lot_spec, floor_to_lot_step
    from risk import compute_lot_for_account
    acc = {"equity": 10_000, "account_type": "standard",
           "symbol_specs": {"XAUUSD": {"volume_min": 0.1, "volume_step": 0.1}}}
    assert lot_spec(acc, "XAUUSD-ECN") == (0.1, 0.1)
    assert lot_spec(acc, "EURUSD") == (0.01, 0.01)
    assert lot_spec({}, "EURUSD") == (0.01, 0.01)
    assert floor_to_lot_step(0.37, 0.1) == pytest.approx(0.3)
    profile = {"risk_pct": 1.0, "tp_atr_mult": 2, "sl_atr_mult": 1, "min_confidence": 60, "kelly_cap": 0}
    # $100 / (50 pips × $10) = 0.20 → floored to the 0.1 step
    out = compute_lot_for_account(acc, "XAUUSD", 4000.0, 3995.0, 70, profile, kelly_enabled=False)
    assert out["sizing_valid"] and out["lot_size"] == pytest.approx(0.2)
    # $100 / (500 pips × $10) = 0.02 < broker min 0.1 → min applies but overshoots budget → rejected
    out = compute_lot_for_account(acc, "XAUUSD", 4000.0, 3950.0, 70, profile, kelly_enabled=False)
    assert out["sizing_valid"] is False and out["method"] == "rejected_min_lot_risk"
    assert "0.1 lot" in out["reject_reason"]


# ------------------------------------------------------------------ R9
def test_r9_no_trim_site_rounds_back_up_to_minimum():
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot_runner.py")).read()
    assert not re.search(r"max\(\s*0\.01\s*,", src), "a scale-down still clamps back up to 0.01"
    assert not re.search(r"floor_to_lot_step\([^\n]*\),\s*0\.01\)", src)
    assert src.count("_skip_below_min(") >= 11


def test_r9_skip_below_min_records_pulse():
    import bot_runner as br
    pulses = []

    async def _pulse(db, cfg, **kw):
        pulses.append(kw)
    with patch.object(br, "_record_pulse", _pulse):
        _arun(br._skip_below_min(None, {}, "EURJPY", 0.004, 0.01, "risk engine"))
    assert pulses and pulses[0]["action"] == "SKIP" and "Below minimum lot" in pulses[0]["reason"]


# ------------------------------------------------------------------ B10


def test_b10_authority_reduction_floors_and_refuses_below_minimum():
    import inspect
    import execution_authority as ea
    src = inspect.getsource(ea)
    assert "floor_to_lot_step(_orig * float(gate[\"reduce_factor\"]), _step)" in src
    assert '"blocked": "below_minimum_lot"' in src
    assert "round(_orig * float(gate[\"reduce_factor\"]), 2)" not in src
    from pip_utils import floor_to_lot_step
    assert floor_to_lot_step(0.03 * 0.5, 0.01) == pytest.approx(0.01)
    assert floor_to_lot_step(0.01 * 0.5, 0.01) == 0.0          # → refused, not 0.01
    assert floor_to_lot_step(0.019 * 0.6, 0.01) == pytest.approx(0.01)   # 0.0114 floors, never rounds to 0.02


# ------------------------------------------------------------------ B11
@pytest.fixture
def db():
    client = MongoClient(os.environ["MONGO_URL"])
    yield client[os.environ["DB_NAME"]]
    client.close()


def test_b11_paper_pnl_in_dollars_and_single_settlement(db):
    import execution as ex
    uid = f"b1-{ObjectId()}"
    acc_id = db.accounts.insert_one({"user_id": uid, "mode": "paper", "balance": 10_000.0,
                                     "equity": 10_000.0, TAG: True}).inserted_id
    tid = db.trades.insert_one({"user_id": uid, "account_id": str(acc_id), "mode": "paper", "status": "open",
                                "symbol": "EURUSD", "action": "BUY", "lot_size": 0.10,
                                "entry_price": 1.1000, "stop_loss": 1.0950, "take_profit": 1.1050,
                                "opened_at": datetime.now(timezone.utc).isoformat(), TAG: True}).inserted_id
    try:
        with patch.object(ex, "get_quote", AsyncMock(return_value={"price": 1.1050})), \
                patch.object(ex.ws_manager, "broadcast", AsyncMock()), \
                patch("notifier.notify_trade_closed", AsyncMock()):
            n1 = _arun(ex.settle_paper_trades_against_price())
            row = db.trades.find_one({"_id": tid})
            assert row["status"] == "closed" and row["close_reason"] == "take_profit"
            # 50 pips × $10/pip/lot × 0.10 lot = $50 (old formula gave 0.0005)
            assert row["pnl"] == pytest.approx(50.0)
            bal_after = db.accounts.find_one({"_id": acc_id})["balance"]
            assert bal_after == pytest.approx(10_050.0)
        assert n1 >= 1
    finally:
        db.trades.delete_many({TAG: True})
        db.accounts.delete_many({TAG: True})


def test_b11_paper_trade_settles_exactly_once():
    """A sweep working from a stale snapshot (row already closed by another sweep)
    must not credit the virtual balance a second time."""
    import execution as ex
    fake = MagicMock()
    row = {"_id": ObjectId(), "user_id": "u", "account_id": str(ObjectId()), "symbol": "EURUSD",
           "action": "BUY", "lot_size": 0.1, "entry_price": 1.1, "stop_loss": 1.095, "take_profit": 1.105}
    fake.trades.find.return_value.to_list = AsyncMock(return_value=[row])
    fake.trades.update_one = AsyncMock(return_value=MagicMock(modified_count=0))   # someone closed it first
    fake.accounts.update_one = AsyncMock()
    with patch.object(ex, "get_db", return_value=fake), \
            patch.object(ex, "get_quote", AsyncMock(return_value={"price": 1.105})), \
            patch.object(ex.ws_manager, "broadcast", AsyncMock()) as bc:
        n = _arun(ex.settle_paper_trades_against_price())
    assert n == 0
    filt = fake.trades.update_one.await_args.args[0]
    assert filt["status"] == "open"
    fake.accounts.update_one.assert_not_awaited()
    bc.assert_not_awaited()
