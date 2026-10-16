"""#43 — VaR deleverage: (a) inputs audited, a 0.25 %-risk position never breaches the cap; (#8 widened) breakeven_trigger_r
for every non-scalp engine. Pure / FakeDb-free unit tests; the close-command dispatch + watchdog e2e lives in
tests/integration/test_v1_60_54_close_watchdog.py."""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _gold(lot=0.05, entry=4000.0, sl=3999.0, symbol="XAUUSD", **kw):
    return {"_id": "t1", "symbol": symbol, "action": "BUY", "lot_size": lot, "entry_price": entry, "stop_loss": sl,
            "status": "open", **kw}


# ----------------------------------------------------------------------------------------------- (a) VaR inputs
def test_open_risk_falls_back_to_original_stop():
    from portfolio.var import open_risk_usd
    assert open_risk_usd(_gold(sl=None, original_stop_loss=3999.0)) == pytest.approx(5.0)   # $1 × 0.05 × 100
    assert open_risk_usd(_gold(sl=None)) is None
    assert open_risk_usd(_gold(sl=4001.0)) == 0.0                                            # BE → zero open risk


def test_var_is_independent_of_pip_value_and_account_type(monkeypatch):
    """The #42 ×1000 pip-value error cannot inflate VaR: notional comes from the instrument table (XAU 100 oz),
    never from pip_usd / account_type."""
    import portfolio.var as pv
    monkeypatch.setattr(pv, "_atr_pct", _none)
    a = _run(pv.calculate_var([_gold()], equity=10_000.0))
    b = _run(pv.calculate_var([_gold(symbol="GOLD#", account_type="microcent")], equity=10_000.0))
    assert a["positions"][0]["notional"] == pytest.approx(20_000.0) == b["positions"][0]["notional"]
    assert a["var_95_usd"] == b["var_95_usd"]


async def _none(_sym):
    return None


def test_single_quarter_percent_risk_position_never_breaches_var_cap(monkeypatch):
    """600007662145: 0.05 lot XAUUSD, 10-pip stop ($5 = 0.05 % of $10k; even a $25 / 0.25 % stop) — the stop bound
    (×1.5 gap) is far inside the 5 % cap, with or without ATR history, so var_cap never triggers."""
    import portfolio.var as pv
    from portfolio.risk_manager import VAR_CAP_PCT, var_cap_exempt_reason
    monkeypatch.setattr(pv, "_atr_pct", _none)                 # 1 % fallback vol → parametric $329 = 3.3 %
    for sl in (3999.0, 3995.0):                                 # $5 and $25 open risk
        pos = _gold(sl=sl)
        var = _run(pv.calculate_var([pos], equity=10_000.0))
        assert var["var_95_pct_equity"] <= VAR_CAP_PCT
        assert var["stop_bounded_loss_usd"] == pytest.approx(abs(4000 - sl) * 0.05 * 100 * pv.STOP_GAP_FACTOR)
        assert var_cap_exempt_reason([pos], 10_000.0, {"risk_level": "low"}) is not None
    # tiny equity: parametric 1 % × $20k = $329 → 33 % of $1k, but the stop bounds it to $37.5 = 3.75 %
    var = _run(pv.calculate_var([_gold(sl=3995.0)], equity=1_000.0))
    assert var["var_95_pct_equity"] == pytest.approx(3.75)


def test_mixed_bound_when_one_position_has_no_stop(monkeypatch):
    import portfolio.var as pv
    monkeypatch.setattr(pv, "_atr_pct", _none)
    stopped, naked = _gold(sl=3995.0), {**_gold(sl=None), "_id": "t2", "symbol": "EURUSD", "lot_size": 0.1, "entry_price": 1.1}
    var = _run(pv.calculate_var([stopped, naked], equity=10_000.0))
    assert var["stop_bounded_loss_usd"] is not None and any("mixed bound" in n for n in var["notes"])
    # stopped leg: $25 × 1.5 = $37.5; naked leg keeps its parametric share — bound is below the pure parametric figure
    assert var["stop_bounded_loss_usd"] > 37.5


def test_book_inside_var_cap_is_exempt_and_breakeven_book_is_exempt():
    from portfolio.risk_manager import var_cap_exempt_reason
    two = [_gold(sl=3995.0), {**_gold(sl=3995.0), "_id": "t2"}]           # $50 × 1.5 = 0.75 % of $10k
    assert "within the VaR cap" in var_cap_exempt_reason(two, 10_000.0, {"risk_level": "low"})
    assert "zero open risk" in var_cap_exempt_reason([_gold(sl=4001.0)], 10_000.0, None)
    big = [_gold(lot=1.0, sl=3900.0), {**_gold(lot=1.0, sl=3900.0), "_id": "t2"}]   # $20k × 1.5 of $10k equity
    assert var_cap_exempt_reason(big, 10_000.0, {"risk_level": "low"}) is None


def test_sector_exposure_uses_instrument_notional_for_broker_symbols():
    from portfolio.risk_manager import _sector_exposure
    from portfolio.sectors import sector_for
    assert sector_for("GOLD#") == sector_for("XAUUSD+") == sector_for("XAUUSD") == "commodity"   # not "other" (100 % cap)
    out = _run(_sector_exposure([_gold(symbol="GOLD#")], 10_000.0))
    assert out["commodity"]["notional"] == pytest.approx(20_000.0)


def test_deleverage_closes_ride_the_fenced_full_close_path():
    src = open(os.path.join(BACKEND, "portfolio", "risk_manager.py")).read()
    i = src.index("async def execute_deleveraging_actions")
    assert 'pending_modification={"type": "FULL_CLOSE", "reason": reason}, emergency=True' in src[i:]
    br = open(os.path.join(BACKEND, "routes", "bridge_routes.py")).read()
    assert "record_terminal_ack(db, trade, success=bool(payload.success)" in br
    assert "sweep_close_watchdog(db)" in open(os.path.join(BACKEND, "bot_runner.py")).read()


# ----------------------------------------------------------------------------------------------- (#8) breakeven_trigger_r
class _WS:
    def __init__(self):
        self.sent = []

    async def broadcast(self, uid, kind, payload):
        self.sent.append((kind, payload))


def _breakout(**kw):
    # breakout signal: TP1 = TP (tp_pips all equal) → the TP1 break-even never fires before the TP itself
    return {"_id": "t1", "user_id": "u1", "account_id": "a1", "symbol": "XAUUSD", "action": "BUY", "entry_price": 4000.0,
            "stop_loss": 3995.0, "lot_size": 0.05, "sl_pips": 50, "tp_pips": [150, 150, 150], "scope": "breakout", **kw}


def _patch(monkeypatch, stamped=None):
    import trade_manager as tm
    calls = []

    async def _stamp(db, filt, mod, extra=None):
        calls.append((filt, mod, extra))
        return stamped if stamped is not None else {"intent_id": "i1", **mod}

    async def _noclamp(db, trade, new_sl, current, source):
        return None
    async def _no_db(*a, **k):      # CI unit lane has no MONGO_URL — nothing here may touch get_db()
        raise AssertionError("unexpected DB write")
    monkeypatch.setattr(tm, "stamp_pending_modification", _stamp)
    monkeypatch.setattr(tm, "be_clamped_skip", _noclamp)
    monkeypatch.setattr(tm, "record_sl_loosen_refusal", _no_db)
    ws = _WS()
    monkeypatch.setattr(tm, "ws_manager", ws)
    return calls, ws


def test_breakeven_trigger_r_moves_stop_to_entry_for_breakout(monkeypatch):
    import trade_manager as tm
    calls, ws = _patch(monkeypatch)
    cfg = {"breakeven_trigger_r": 1.0}
    # +0.8R → nothing; +1.0R (50 pips) → MODIFY_SL to entry
    assert _run(tm._breakeven_trigger_r(None, _breakout(), cfg, 4004.0, 40.0)) is False and not calls
    assert _run(tm._breakeven_trigger_r(None, _breakout(), cfg, 4005.0, 50.0)) is True
    filt, mod, extra = calls[0]
    assert mod == {"type": "MODIFY_SL", "new_sl": 4000.0, "reason": "breakeven_trigger_r"}
    assert extra["be_target_sl"] == 4000.0 and extra["be_rule"] == "breakeven_trigger_r:1"
    assert filt["close_requested"] == {"$ne": True}            # never behind a close in flight
    assert ws.sent[0][1]["action"] == "BE_TRIGGER_R"


def test_breakeven_trigger_r_disabled_armed_or_loosening_does_nothing(monkeypatch):
    import trade_manager as tm
    calls, _ = _patch(monkeypatch)
    assert _run(tm._breakeven_trigger_r(None, _breakout(), {"breakeven_trigger_r": 0}, 4010.0, 100.0)) is False
    assert _run(tm._breakeven_trigger_r(None, _breakout(), {}, 4010.0, 100.0)) is False
    assert _run(tm._breakeven_trigger_r(None, _breakout(be_target_sl=4000.0), {"breakeven_trigger_r": 1.0}, 4010.0, 100.0)) is False
    assert _run(tm._breakeven_trigger_r(None, _breakout(breakeven_set=True), {"breakeven_trigger_r": 1.0}, 4010.0, 100.0)) is False
    assert _run(tm._breakeven_trigger_r(None, _breakout(stop_loss=4001.0), {"breakeven_trigger_r": 1.0}, 4010.0, 100.0)) is False
    assert not calls


def test_breakeven_trigger_r_is_a_real_config_field_again():
    from models import BotConfigUpdate as BotConfig, DEAD_CONFIG_FIELDS
    assert "breakeven_trigger_r" not in DEAD_CONFIG_FIELDS
    assert BotConfig.model_fields["breakeven_trigger_r"].default == 1.0
    src = open(os.path.join(BACKEND, "trade_manager.py")).read()
    assert "if not is_scalp_engine_trade(trade) and await _breakeven_trigger_r(" in src   # scalp keeps its +0.5R rule
    ui = open(os.path.join(os.path.dirname(BACKEND), "frontend", "src", "pages", "BotConfig.jsx")).read()
    assert 'field="breakeven_trigger_r"' in ui and "breakeven_trigger_r: cfg.breakeven_trigger_r ?? 1.0" in ui
