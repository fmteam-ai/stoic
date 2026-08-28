"""Tests for iter-63 market-hours hard veto.

Closed forex/metals markets must HOLD unconditionally — even under
aggressive_mode. Crypto is always tradeable. Boundary minutes must be
correct (Fri 20:59 = open, Fri 21:00 = closed, Sun 21:59 = closed,
Sun 22:00 = open).
"""
from datetime import datetime, timezone

import pytest

from microstructure import is_market_closed, is_crypto_symbol


# ─────────────────── helpers ───────────────────
def _at(year, mon, day, hour, minute=0):
    return datetime(year, mon, day, hour, minute, tzinfo=timezone.utc)


# ─────────────────── crypto detection ───────────────────
def test_crypto_symbols_recognised():
    assert is_crypto_symbol("BTCUSD")
    assert is_crypto_symbol("BTC/USDT")
    assert is_crypto_symbol("ETHUSD")
    assert is_crypto_symbol("SOLUSD")
    assert is_crypto_symbol("eth/usdt")  # case-insensitive


def test_non_crypto_symbols_rejected():
    assert not is_crypto_symbol("XAUUSD")
    assert not is_crypto_symbol("EURUSD")
    assert not is_crypto_symbol("XAGUSD")
    assert not is_crypto_symbol("US30")
    # Edge: a typo like 'USDUSD' should NOT slip into crypto bucket
    assert not is_crypto_symbol("USDUSD")


# ─────────────────── XAUUSD weekly close window ───────────────────
@pytest.mark.parametrize("t, expected_open", [
    # Boundaries
    (_at(2026, 6, 22, 12, 0), True),    # Mon noon UTC — open
    (_at(2026, 6, 26, 20, 59), True),   # Fri 20:59 — 1 min before close
    (_at(2026, 6, 26, 21, 0), False),   # Fri 21:00 — closes exactly here
    (_at(2026, 6, 27, 0, 0), False),    # Sat midnight — closed
    (_at(2026, 6, 27, 12, 0), False),   # Sat noon — closed
    (_at(2026, 6, 28, 0, 0), False),    # Sun midnight — closed
    (_at(2026, 6, 28, 21, 59), False),  # Sun 21:59 — 1 min before reopen
    (_at(2026, 6, 28, 22, 0), True),    # Sun 22:00 — reopens exactly here
    (_at(2026, 6, 29, 9, 0), True),     # Mon 09:00 — normal weekday
])
def test_xauusd_weekly_close_window(t, expected_open):
    closure = is_market_closed("XAUUSD", t)
    if expected_open:
        assert closure is None, f"Expected OPEN @ {t}, got {closure}"
    else:
        assert closure is not None, f"Expected CLOSED @ {t}, got open"
        assert "reopens_in_hours" in closure
        assert closure["reopens_in_hours"] >= 0
        assert "reopens_at_utc" in closure


# ─────────────────── crypto never closed ───────────────────
@pytest.mark.parametrize("sym", ["BTCUSD", "ETHUSD", "BTC/USDT", "SOLUSD"])
@pytest.mark.parametrize("t", [
    _at(2026, 6, 27, 12, 0),   # Sat noon
    _at(2026, 6, 28, 4, 0),    # Sun 04:00
    _at(2026, 12, 25, 12, 0),  # Christmas day
])
def test_crypto_always_tradeable(sym, t):
    assert is_market_closed(sym, t) is None


# ─────────────────── other forex/metals follow same window ───────────────────
@pytest.mark.parametrize("sym", ["EURUSD", "GBPUSD", "XAGUSD", "USDJPY"])
def test_forex_metals_follow_same_window(sym):
    # Saturday → closed
    assert is_market_closed(sym, _at(2026, 6, 27, 10, 0)) is not None
    # Wednesday → open
    assert is_market_closed(sym, _at(2026, 6, 24, 10, 0)) is None


# ─────────────────── reopens_in_hours sanity ───────────────────
def test_reopens_in_hours_decreasing_toward_open():
    """At Sat noon UTC, reopen is ~34h away. At Sun noon, ~10h away."""
    sat_noon = is_market_closed("XAUUSD", _at(2026, 6, 27, 12, 0))
    sun_noon = is_market_closed("XAUUSD", _at(2026, 6, 28, 12, 0))
    assert sat_noon["reopens_in_hours"] > sun_noon["reopens_in_hours"]
    # Sun 22:00 reopen — Sat noon = 34h, Sun noon = 10h
    assert 30 <= sat_noon["reopens_in_hours"] <= 36
    assert 8 <= sun_noon["reopens_in_hours"] <= 12


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
