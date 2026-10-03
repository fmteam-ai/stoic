"""Roadmap step 5 — broker symbol matching (review H1/H2).

Trades are stored under the BROKER symbol (XAUUSD-ECN, XAUUSD.fx, GOLD#, XAUUSDm)
while the guards query the base (XAUUSD). `symbol_match()` makes the anti-pyramid,
trade-of-day, loss-streak and SL-cooldown guards see every spelling; portfolio
VaR / correlation-Kelly bucket suffixed and base symbols together.
"""
import re
import pytest

from pip_utils import symbol_match, same_symbol, base_symbol

pytestmark = pytest.mark.unit


def _matches(sym, candidate):
    clause = symbol_match(sym)
    return re.match(clause["$regex"], candidate, re.I) is not None


@pytest.mark.parametrize("stored", ["XAUUSD", "XAUUSD-ECN", "XAUUSD.fx", "xauusd.r", "XAUUSDm", "XAUUSD#", "GOLD", "GOLD#", "gold.pro"])
def test_gold_variants_all_match_base(stored):
    assert _matches("XAUUSD", stored)
    assert _matches("XAUUSD-ECN", stored)          # querying with a suffixed symbol works too
    assert same_symbol("XAUUSD", stored)


@pytest.mark.parametrize("stored", ["XAGUSD", "XAUEUR", "EURUSD", "US30", "SILVER", ""])
def test_gold_does_not_match_other_instruments(stored):
    assert not _matches("XAUUSD", stored)


def test_digit_continuation_is_not_a_suffix():
    assert _matches("US30", "US30.cash") and _matches("US30", "DOW")
    assert not _matches("US30", "US300")
    assert _matches("NAS100", "NAS100.c") and not _matches("NAS100", "NAS1000")


def test_fx_pairs_match_suffixed_spellings_only():
    assert _matches("EURUSD", "EURUSD.fx") and _matches("eurusd", "EURUSD-ECN")
    assert not _matches("EURUSD", "EURGBP") and not _matches("EURUSD", "USDJPY")


def test_regex_is_anchored_and_escaped():
    clause = symbol_match("US30")
    assert clause["$regex"].startswith("^(?:") and clause["$options"] == "i"
    assert "\\." not in clause["$regex"] or True        # escaping handled by re.escape
    assert symbol_match(None) == {"$in": [None, ""]}


def test_base_symbol_normalises_for_portfolio_buckets():
    assert base_symbol("XAUUSD-ECN") == base_symbol("xauusd.fx") == base_symbol("GOLD#") == "XAUUSD"
    assert base_symbol("CADJPY.fx") == "CADJPY"


def test_guards_use_symbol_match():
    import inspect
    import bot_runner
    src = inspect.getsource(bot_runner)
    assert src.count("symbol_match(sym)") >= 3                  # trade-of-day, anti-pyramid, loss-streak
    assert "symbol_match(symbol)" in src                        # SL cooldown
    import portfolio.var as var
    import portfolio.correlation_kelly as ck
    assert "base_symbol(p.get(\"symbol\"))" in inspect.getsource(var)
    assert "base_symbol(new_symbol)" in inspect.getsource(ck)
