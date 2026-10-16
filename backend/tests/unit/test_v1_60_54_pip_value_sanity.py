"""#42 — RoboForex XAUUSD pip value ×1000 off: broker-spec pip value, ×3 sanity guard vs the instrument table,
raw-lot anomaly hard reject, provenance on the decision."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GOLD_SPEC = {"contract_size": 100.0, "tick_value": 1.0, "tick_size": 0.01, "point": 0.01, "digits": 2}
EUR_SPEC = {"contract_size": 100000.0, "tick_value": 1.0, "tick_size": 0.00001, "point": 0.00001, "digits": 5}


def _acct(broker, account_type="standard", specs=None, mode="live"):
    return {"_id": f"{broker}-1", "label": broker, "broker": broker, "account_type": account_type, "mode": mode,
            "symbol_specs": specs or {}, "equity": 10_000.0}


@pytest.mark.parametrize("broker,symbol", [("STARTRADER", "XAUUSD+"), ("OnEquity", "GOLD#"), ("VTMarkets", "XAUUSD+"),
                                           ("RoboForex", "XAUUSD")])
def test_gold_broker_specs_give_10_usd_per_pip(broker, symbol):
    """EA specs are keyed by BASE symbol; tick_value 1.0 per 0.01 → $10 per 0.10 pip per lot on every broker."""
    from risk import broker_pip_value_usd_per_lot, resolve_pip_value
    acct = _acct(broker, specs={"XAUUSD": GOLD_SPEC})
    assert broker_pip_value_usd_per_lot(symbol, acct) == pytest.approx(10.0)
    pv = resolve_pip_value(symbol, acct, price=4000.0)
    assert pv["pip_usd"] == pytest.approx(10.0) and pv["source"] == "broker_spec" and pv["anomaly"] is False


def test_roboforex_incident_account_type_derived_0_01_is_overridden():
    """16 Oct 08:19 trade 600007662145: $0.01/pip for XAUUSD (×1000 off) → raw 25.14 lots. Now → table $10 + anomaly."""
    from risk import resolve_pip_value, compute_lot_for_account
    acct = _acct("RoboForex", account_type="microcent")            # no spec → account_type table → $0.01
    pv = resolve_pip_value("XAUUSD", acct, price=4000.0)
    assert pv["candidate"] == pytest.approx(0.01) and pv["pip_usd"] == pytest.approx(10.0)
    assert pv["source"] == "table_override" and pv["anomaly"] is True and "÷1000" in pv["note"]
    profile = {"risk_pct": 0.25, "tp_atr_mult": 2.0, "sl_atr_mult": 1.0, "min_confidence": 50, "kelly_cap": 0.0}
    sized = compute_lot_for_account(acct, "XAUUSD", 4000.0, 3999.0, 70, profile)      # 10 pips
    assert sized["sizing_valid"] and sized["pip_usd_per_lot"] == 10.0 and sized["lot_size"] == pytest.approx(0.25)
    assert sized["pip_value_source"] == "table_override" and sized["pip_value_anomaly"] is True


def test_roboforex_cent_spec_is_overridden_too():
    from risk import resolve_pip_value
    acct = _acct("RoboForex", specs={"XAUUSD": {**GOLD_SPEC, "tick_value": 0.001, "contract_size": 0.1}})
    pv = resolve_pip_value("XAUUSD", acct, price=4000.0)
    assert pv["candidate"] == pytest.approx(0.01) and pv["pip_usd"] == 10.0 and pv["anomaly"] is True


def test_roboforex_standard_without_spec_uses_table_no_anomaly():
    from risk import resolve_pip_value
    pv = resolve_pip_value("XAUUSD", _acct("RoboForex"), price=4000.0)
    assert pv["pip_usd"] == 10.0 and pv["source"] == "account_type_table" and pv["anomaly"] is False


def test_eurusd_spec_and_mislabelled_account():
    from risk import broker_pip_value_usd_per_lot, resolve_pip_value
    acct = _acct("RoboForex", specs={"EURUSD": EUR_SPEC})
    assert broker_pip_value_usd_per_lot("EURUSD#", acct) == pytest.approx(10.0)
    assert resolve_pip_value("EURUSD", acct, price=1.1)["source"] == "broker_spec"
    bad = resolve_pip_value("EURUSD", _acct("RoboForex", account_type="cent"), price=1.1)
    assert bad["candidate"] == pytest.approx(0.1) and bad["pip_usd"] == 10.0 and bad["anomaly"] is True


def test_within_x3_broker_value_is_kept():
    from risk import resolve_pip_value
    acct = _acct("X", specs={"XAUUSD": {**GOLD_SPEC, "tick_value": 0.5}})      # $5/pip — within ×3
    pv = resolve_pip_value("XAUUSD", acct, price=4000.0)
    assert pv["pip_usd"] == pytest.approx(5.0) and pv["anomaly"] is False


def test_paper_microcent_account_keeps_its_convention():
    from risk import resolve_pip_value
    pv = resolve_pip_value("XAUUSD", _acct("INTERNAL_PAPER", account_type="microcent", mode="paper"), price=4000.0)
    assert pv["pip_usd"] == pytest.approx(0.01) and pv["anomaly"] is False


def test_raw_lot_anomaly_hard_reject():
    from risk import raw_lot_anomaly
    assert raw_lot_anomaly(25.14, 0.05) is True          # the incident's raw lot vs max 0.05
    assert raw_lot_anomaly(0.5, 0.05) is False           # exactly 10× is still allowed
    assert raw_lot_anomaly(0.51, 0.05) is True
    assert raw_lot_anomaly(25.0, 0) is False             # no max lot → no reference
    src = open(os.path.join(BACKEND, "bot_runner.py")).read()
    i = src.index("raw_lot_anomaly(absolute_lot, max_lot_cap)")
    assert i < src.index("max_cap_kelly_scaled") and "Sizing anomaly" in src[i:i + 800]   # before any trim
    assert "sizing_anomaly_block" in src and 'raise_alert(db, "pip_value_anomaly"' in src
    assert '"pip_value_source"' in open(os.path.join(BACKEND, "trade_decisions.py")).read()
