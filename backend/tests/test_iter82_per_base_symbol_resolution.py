"""iter-82 · Per-base symbol resolution for mixed-convention brokers.

The "one suffix per broker" model (iter-76) breaks on brokers like VTMarkets
where forex pairs are bare (`EURUSD`) but gold is `XAUUSD-ECN`. The detector
correctly votes for suffix="" based on 5 bare forex matches; the bot then
sends bare `XAUUSD` for gold and the broker rejects it.

`resolve_broker_symbol()` fixes this by consulting the actual MarketWatch
inventory for a per-base match BEFORE falling back to the broker-wide suffix.
"""
from __future__ import annotations

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker_symbol_detector import resolve_broker_symbol  # noqa: E402


# ─────────────── VTMarkets (the bug we hit) ───────────────
VT_SYMBOLS = ["AUDUSD", "EURUSD", "GBPUSD", "USDCAD", "USDJPY", "XAUUSD-ECN"]


def test_vtmarkets_gold_resolves_to_ecn():
    """Forex bare, gold with -ECN — exactly the live VTMarkets account."""
    assert resolve_broker_symbol("XAUUSD", VT_SYMBOLS, suffix_fallback="") == "XAUUSD-ECN"


def test_vtmarkets_forex_stays_bare():
    """EURUSD on VT should stay bare even though gold has a suffix."""
    assert resolve_broker_symbol("EURUSD", VT_SYMBOLS, suffix_fallback="") == "EURUSD"


def test_vtmarkets_missing_base_returns_none():
    """BTCUSD isn't in VT's MarketWatch → return None so execution
    refuses to send a doomed order."""
    assert resolve_broker_symbol("BTCUSD", VT_SYMBOLS, suffix_fallback="") is None


# ─────────────── Tauro-style `.fx` consistent broker ───────────────
TAURO_SYMBOLS = ["EURUSD.fx", "GBPUSD.fx", "USDJPY.fx", "XAUUSD.fx", "BTCUSD.fx"]


def test_tauro_consistent_suffix_resolves_all():
    assert resolve_broker_symbol("XAUUSD", TAURO_SYMBOLS, suffix_fallback=".fx") == "XAUUSD.fx"
    assert resolve_broker_symbol("BTCUSD", TAURO_SYMBOLS, suffix_fallback=".fx") == "BTCUSD.fx"


# ─────────────── OnEquity (no gold offered) ───────────────
ONEQUITY_SYMBOLS = [
    "EURUSD#", "GBPUSD#", "USDJPY#", "USDCHF#", "USDCAD#",
    "AUDUSD#", "NZDUSD#", "EURGBP#", "EURJPY#", "GBPJPY#",
    "GER40Cash", "UK100Cash", "BTCUSD", "ETHUSD",
]


def test_onequity_gold_unavailable_returns_none():
    """OnEquity doesn't list any gold ticker → resolver returns None →
    execution layer refuses to send the order (no more symbol_not_found
    failures)."""
    assert resolve_broker_symbol("XAUUSD", ONEQUITY_SYMBOLS, suffix_fallback="#") is None


def test_onequity_btc_stays_bare():
    """BTCUSD is bare on OnEquity despite # forex suffix — resolver
    should pick the exact match without forcing the # suffix."""
    assert resolve_broker_symbol("BTCUSD", ONEQUITY_SYMBOLS, suffix_fallback="#") == "BTCUSD"


def test_onequity_eurusd_resolves_with_hash():
    assert resolve_broker_symbol("EURUSD", ONEQUITY_SYMBOLS, suffix_fallback="#") == "EURUSD#"


# ─────────────── Fallback path — EA hasn't reported yet ───────────────
def test_fallback_to_broker_wide_suffix_when_marketwatch_unknown():
    """When `available_symbols` is None (EA hasn't reported), we fall back
    to base + broker-wide suffix. Same behaviour as iter-76."""
    assert resolve_broker_symbol("XAUUSD", None, suffix_fallback=".fx") == "XAUUSD.fx"
    assert resolve_broker_symbol("XAUUSD", None, suffix_fallback="") == "XAUUSD"
    assert resolve_broker_symbol("XAUUSD", [], suffix_fallback=".fx") == "XAUUSD.fx"


def test_fallback_with_empty_suffix_returns_bare():
    assert resolve_broker_symbol("BTCUSD", None, suffix_fallback="") == "BTCUSD"


# ─────────────── Edge cases ───────────────
def test_prefers_shortest_suffix_when_multiple_variants():
    """If a broker offers both `XAUUSD` and `XAUUSDmicro`, prefer the
    plain one — shortest tail wins."""
    syms = ["XAUUSD", "XAUUSDmicro", "EURUSD"]
    assert resolve_broker_symbol("XAUUSD", syms, suffix_fallback="") == "XAUUSD"


def test_empty_base_returns_none():
    assert resolve_broker_symbol("", VT_SYMBOLS, suffix_fallback="") is None


def test_handles_non_string_entries_gracefully():
    """Robustness: shouldn't crash if MarketWatch list has None or numbers."""
    syms = [None, 123, "EURUSD", "", "XAUUSD-ECN"]
    assert resolve_broker_symbol("XAUUSD", syms, suffix_fallback="") == "XAUUSD-ECN"


def test_case_insensitive_match():
    assert resolve_broker_symbol("xauusd", ["XAUUSD.fx", "EURUSD.fx"], suffix_fallback="") == "XAUUSD.fx"
