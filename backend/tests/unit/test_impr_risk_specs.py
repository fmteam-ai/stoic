"""Instrument spec service — EA-reported vs static fallback, pip value /
notional across asset classes, live-FX conversion, spec-aware sizing.
Pure unit tests: no database, no network (in-memory doubles only)."""
import asyncio
from datetime import datetime, timezone

import pytest

import instrument_specs as isp
from instrument_specs import (InstrumentSpec, get_spec, notional_usd,
                              pip_value_usd, spec_from_account, static_spec,
                              usd_per_unit)
from risk import compute_lot_for_account, get_profile

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])

    async def find_one(self, flt, proj=None, sort=None):
        rows = [d for d in self.docs
                if all(d.get(k) == v for k, v in flt.items())]
        if sort:
            key, direction = sort[0]
            rows.sort(key=lambda d: d.get(key), reverse=direction < 0)
        return dict(rows[0]) if rows else None


class _DB:
    def __init__(self, accounts=(), ticks=()):
        self.accounts = _Coll(accounts)
        self.price_ticks = _Coll(ticks)


EA_ACCOUNT = {
    "_id": "acct-ea", "broker_currency_reported": "USD",
    "symbol_specs_updated_at": datetime.now(timezone.utc).isoformat(),
    "symbol_specs": {
        "XAUUSD": {"point": 0.01, "digits": 2, "stops_level_points": 20,
                   "freeze_level_points": 5, "trade_mode": 4,
                   "tick_size": 0.01, "tick_value": 1.0,
                   "contract_size": 100.0, "volume_min": 0.01,
                   "volume_max": 50.0, "volume_step": 0.01},
        "US30": {"point": 0.01, "digits": 2, "stops_level_points": 0,
                 "freeze_level_points": 0, "tick_size": 0.01,
                 "tick_value": 0.1, "contract_size": 10.0,
                 "volume_min": 0.1, "volume_max": 20.0, "volume_step": 0.1},
        # pre-v1.54 EA: stop levels only, no contract economics
        "EURUSD": {"point": 0.00001, "digits": 5, "stops_level_points": 10,
                   "freeze_level_points": 0},
    },
}


# ── spec resolution ─────────────────────────────────────────────────────
def test_static_fallback_when_no_ea_specs():
    sp = spec_from_account({}, "EURUSD.r")
    assert sp.source == "static" and sp.symbol == "EURUSD"
    assert sp.contract_size == 100_000 and sp.pip_size == 0.0001
    assert (sp.base_currency, sp.profit_currency) == ("EUR", "USD")
    assert sp.volume_step == 0.01 and sp.volume_min == 0.01


def test_ea_reported_spec_wins():
    sp = spec_from_account(EA_ACCOUNT, "XAUUSD-ECN")
    assert sp.source == "ea" and sp.from_broker
    assert sp.contract_size == 100.0 and sp.tick_value == 1.0
    assert sp.stops_level == 20 and sp.freeze_level == 5
    assert sp.volume_max == 50.0 and sp.trade_mode == 4
    assert sp.tick_value_currency == "USD"


def test_pre_v154_spec_keeps_static_economics():
    sp = spec_from_account(EA_ACCOUNT, "EURUSD")
    assert sp.source == "static"          # no tick_value/contract from EA
    assert sp.stops_level == 10 and sp.point == 0.00001
    assert pip_value_usd(sp, 1.0, 1.1) == pytest.approx(10.0)


def test_get_spec_loads_account_and_falls_back():
    db = _DB(accounts=[EA_ACCOUNT])
    assert _run(get_spec(db, "acct-ea", "US30")).source == "ea"
    assert _run(get_spec(db, "acct-ea", "BTCUSD")).source == "static"
    assert _run(get_spec(db, "missing", "XAUUSD")).source == "static"

    class _Boom:
        class accounts:  # noqa: N801
            @staticmethod
            async def find_one(*a, **k):
                raise RuntimeError("db down")
    assert _run(get_spec(_Boom(), "x", "XAUUSD")).source == "static"


# ── pip value / notional (static) ───────────────────────────────────────
@pytest.mark.parametrize("sym,lots,price,pip_usd,notional", [
    ("EURUSD", 1.0, 1.10, 10.0, 110_000.0),
    ("USDJPY", 1.0, 150.0, 100_000 * 0.01 / 150.0, 100_000.0),
    ("XAUUSD", 1.0, 2000.0, 10.0, 200_000.0),
    ("US30", 0.5, 40_000.0, 0.5, 20_000.0),
    ("BTCUSD", 0.1, 60_000.0, 0.1, 6_000.0),
])
def test_static_pip_value_and_notional(sym, lots, price, pip_usd, notional):
    sp = static_spec(sym)
    assert pip_value_usd(sp, lots, price) == pytest.approx(pip_usd)
    assert notional_usd(sp, lots, price) == pytest.approx(notional)


def test_static_matches_legacy_tables_exactly():
    from instruments import notional_usd as legacy_notional
    from pip_utils import pip_value_usd_per_lot
    for sym, px in (("EURUSD", 1.1), ("USDCHF", 0.8), ("GBPJPY", 190.0),
                    ("XAGUSD", 30.0), ("NAS100", 18000.0),
                    ("ETHUSD", 3000.0), ("EURGBP", 0.85)):
        sp = static_spec(sym)
        assert pip_value_usd(sp, 1.0, px) == pytest.approx(
            pip_value_usd_per_lot(sym, "standard", price=px))
        assert notional_usd(sp, 2.0, px) == pytest.approx(
            legacy_notional(sym, 2.0, px))


def test_live_fx_corrects_crosses_and_jpy():
    jpy = 1 / 155.0
    assert pip_value_usd(static_spec("USDJPY"), 1.0, fx=jpy) == \
        pytest.approx(1000 * jpy)
    # EURGBP: pip in GBP → USD via fx; notional in GBP × fx
    gbp = 1.25
    sp = static_spec("EURGBP")
    assert pip_value_usd(sp, 1.0, 0.85, fx=gbp) == pytest.approx(12.5)
    assert notional_usd(sp, 1.0, 0.85, fx=gbp) == \
        pytest.approx(100_000 * 0.85 * gbp)


# ── pip value / notional (EA-reported) ──────────────────────────────────
def test_ea_pip_value_and_notional():
    xau = spec_from_account(EA_ACCOUNT, "XAUUSD")
    # tick 0.01 = $1/lot → pip 0.10 = $10/lot
    assert pip_value_usd(xau, 1.0, 2000.0) == pytest.approx(10.0)
    assert notional_usd(xau, 1.0, 2000.0) == pytest.approx(200_000.0)
    us30 = spec_from_account(EA_ACCOUNT, "US30")
    # broker contract 10 (static assumes 1): pip 1.0 = 0.1 × 100 ticks
    assert pip_value_usd(us30, 1.0, 40_000.0) == pytest.approx(10.0)
    assert notional_usd(us30, 1.0, 40_000.0) == pytest.approx(400_000.0)


def test_ea_tick_value_in_non_usd_deposit_converted_with_fx():
    acct = {**EA_ACCOUNT, "broker_currency_reported": "EUR"}
    sp = spec_from_account(acct, "XAUUSD")
    assert sp.tick_value_currency == "EUR"
    assert pip_value_usd(sp, 1.0, fx=1.1) == pytest.approx(11.0)
    # sizing refuses a non-USD deposit tick value without an FX rate
    assert isp.sizing_pip_value_usd(acct, sp) is None
    assert isp.sizing_pip_value_usd(acct, sp, fx=1.1) == pytest.approx(11.0)


# ── FX conversion ───────────────────────────────────────────────────────
def test_usd_per_unit_from_stored_ticks_then_static():
    now = datetime.now(timezone.utc)
    db = _DB(ticks=[{"symbol": "EURUSD", "price": 1.12, "ts": now},
                    {"symbol": "USDJPY", "price": 160.0, "ts": now}])
    assert _run(usd_per_unit(db, "USD")) == 1.0
    assert _run(usd_per_unit(db, "EUR", allow_network=False)) == \
        pytest.approx(1.12)
    assert _run(usd_per_unit(db, "JPY", allow_network=False)) == \
        pytest.approx(1 / 160.0)
    assert _run(usd_per_unit(db, "GBP", allow_network=False)) == \
        isp.STATIC_USD_PER_UNIT["GBP"]
    assert _run(usd_per_unit(db, "XYZ", allow_network=False)) == 0.0


def test_usd_per_unit_ignores_stale_ticks():
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    db = _DB(ticks=[{"symbol": "EURUSD", "price": 9.99, "ts": old}])
    assert _run(usd_per_unit(db, "EUR", allow_network=False)) == \
        isp.STATIC_USD_PER_UNIT["EUR"]


# ── spec-aware sizing ───────────────────────────────────────────────────
_PROFILE = get_profile("medium")


def _size(account, sym="XAUUSD", entry=2000.0, sl=1990.0, **kw):
    return compute_lot_for_account(account, sym, entry, sl, 70.0, _PROFILE,
                                   **kw)


def test_sizing_unchanged_without_broker_spec():
    base = {"equity": 10_000.0, "account_type": "standard"}
    out = _size(base)
    # 1% of 10k = $100; 100 pips × $10 → 0.10 lot
    assert out["lot_size"] == pytest.approx(0.10)
    assert out["spec_source"] == "static"


def test_sizing_uses_broker_pip_value_and_volume_step():
    acct = {**EA_ACCOUNT, "equity": 100_000.0, "account_type": "standard"}
    # US30: $1000 budget, 100-pt stop, broker pip $10/lot → 1.0 lot
    out = _size(acct, "US30", 40_000.0, 39_900.0)
    assert out["spec_source"] == "ea"
    assert out["pip_usd_per_lot"] == pytest.approx(10.0)
    assert out["lot_size"] == pytest.approx(1.0)
    # same account, odd budget → floored to the BROKER 0.1 step
    acct2 = {**acct, "equity": 135_000.0}
    out2 = _size(acct2, "US30", 40_000.0, 39_900.0)
    assert out2["lot_size"] == pytest.approx(1.3) and out2["volume_step"] == 0.1


def test_sizing_clamps_to_broker_volume_max():
    acct = {**EA_ACCOUNT, "equity": 50_000_000.0}
    out = _size(acct, "XAUUSD", 2000.0, 1990.0)
    assert out["lot_size"] == pytest.approx(50.0)


def test_sizing_cent_account_ignores_broker_tick_value():
    acct = {**EA_ACCOUNT, "equity": 10_000.0, "account_type": "cent"}
    out = _size(acct)
    assert out["spec_source"] == "static"


def test_explicit_spec_kwarg_backward_compatible():
    sp = InstrumentSpec(symbol="XAUUSD", contract_size=100, tick_size=0.01,
                        tick_value=2.0, point=0.01, digits=2,
                        source="ea", pip_size=0.1,
                        extra={"volume_from_broker": True})
    out = _size({"equity": 10_000.0}, spec=sp)
    assert out["pip_usd_per_lot"] == pytest.approx(20.0)
    assert out["lot_size"] == pytest.approx(0.05)
