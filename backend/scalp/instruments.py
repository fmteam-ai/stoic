"""Scalp subsystem · Step 1 — narrow, explicitly-approved trading universe.

EURUSD only until live validation passes (user decision). GBPUSD/USDJPY may
be added AFTER EURUSD proves positive out-of-sample expectancy.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ScalpInstrumentConfig:
    symbol: str
    contract_size: float
    pip_size: float
    tick_size: float
    lot_step: float
    min_lot: float
    max_spread_pips: float
    max_expected_slippage_pips: float
    max_quote_age_ms: int
    allowed_sessions: tuple
    news_blackout_minutes: int
    # session window, UTC hours [start, end)
    session_start_utc: int = 7
    session_end_utc: int = 20


APPROVED = {
    "EURUSD": ScalpInstrumentConfig(
        symbol="EURUSD",
        contract_size=100_000.0,
        pip_size=0.0001,
        tick_size=0.00001,
        lot_step=0.01,
        min_lot=0.01,
        max_spread_pips=1.2,
        max_expected_slippage_pips=0.3,
        max_quote_age_ms=2500,
        allowed_sessions=("london", "newyork_overlap", "newyork"),
        news_blackout_minutes=15,
        session_start_utc=7,
        session_end_utc=20,
    ),
}


def approved(symbol: str) -> ScalpInstrumentConfig | None:
    return APPROVED.get((symbol or "").upper())
