"""Sector classification — closed vocabulary mapping symbol → sector.

Used by the Portfolio Manager for sector exposure caps. Lean and
deterministic — no external lookup, just a curated map of the assets
STOIC actually trades + sensible defaults for the long tail.
"""

# Sector codes used throughout the portfolio manager.
SECTOR_COMMODITY    = "commodity"
SECTOR_CRYPTO       = "crypto"
SECTOR_FX_MAJOR     = "fx_major"
SECTOR_FX_MINOR     = "fx_minor"
SECTOR_EQUITY_INDEX = "equity_index"

_SYMBOL_SECTOR = {
    # Commodities
    "XAUUSD": SECTOR_COMMODITY,
    "XAGUSD": SECTOR_COMMODITY,
    "WTIUSD": SECTOR_COMMODITY,
    "USOIL":  SECTOR_COMMODITY,
    # Crypto
    "BTCUSD": SECTOR_CRYPTO,
    "ETHUSD": SECTOR_CRYPTO,
    "SOLUSD": SECTOR_CRYPTO,
    # FX majors
    "EURUSD": SECTOR_FX_MAJOR,
    "GBPUSD": SECTOR_FX_MAJOR,
    "USDJPY": SECTOR_FX_MAJOR,
    "USDCHF": SECTOR_FX_MAJOR,
    "USDCAD": SECTOR_FX_MAJOR,
    "AUDUSD": SECTOR_FX_MAJOR,
    "NZDUSD": SECTOR_FX_MAJOR,
    # Equity indices
    "US500":  SECTOR_EQUITY_INDEX,
    "NAS100": SECTOR_EQUITY_INDEX,
    "US30":   SECTOR_EQUITY_INDEX,
    "SPX500": SECTOR_EQUITY_INDEX,
}


def sector_for(symbol: str) -> str:
    sym = (symbol or "").upper().strip()
    if sym in _SYMBOL_SECTOR:
        return _SYMBOL_SECTOR[sym]
    # Heuristic fallback — anything ending in USD with letters before is fx_minor
    if sym.endswith("USD") and len(sym) == 6:
        return SECTOR_FX_MINOR
    return "other"


def all_sectors() -> list[str]:
    return [SECTOR_COMMODITY, SECTOR_CRYPTO, SECTOR_FX_MAJOR,
            SECTOR_FX_MINOR, SECTOR_EQUITY_INDEX, "other"]
