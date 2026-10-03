"""iter-144 · Canonical instrument registry (quant review C4).

Single source of truth for contract size, pip size, min volume and lot step
— and USD notional computed through it. The old notional heuristic
(`lot × entry × (100 if gold else 1)`) understated FX exposure by ~100,000×
which silently gutted portfolio VaR/CVaR and every correlation budget.

Notional convention (account currency assumed USD):
  • Registry symbols (metals/crypto/indices): lot × contract_size × price.
  • FX with USD quote (EURUSD): lot × 100,000 × price.
  • FX with USD base (USDJPY): lot × 100,000 (the base IS USD).
  • USD-less crosses (EURGBP, EURJPY): lot × 100,000 × USD-per-base-currency
    (QUOTE_USD_APPROX table) — fix plan B1/R1; the old `× price` was the
    notional in the QUOTE currency (≈150× too high for JPY crosses).
"""
from pip_utils import base_symbol, pip_size

FX_CONTRACT_UNITS = 100_000.0

SPECS = {
    "XAUUSD": {"contract_size": 100.0},
    "XAGUSD": {"contract_size": 5000.0},
    "BTCUSD": {"contract_size": 1.0},
    "ETHUSD": {"contract_size": 1.0},
    "US30":   {"contract_size": 1.0},
    "NAS100": {"contract_size": 1.0},
    "SPX500": {"contract_size": 1.0},
    "US500":  {"contract_size": 1.0},
    "GER40":  {"contract_size": 1.0},
    "USOIL":  {"contract_size": 100.0},
}

_CCY = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SGD",
        "SEK", "NOK", "ZAR", "MXN", "PLN", "TRY", "HKD", "CNH"}


def _is_fx(base: str) -> bool:
    return (len(base) == 6 and base[:3] in _CCY and base[3:] in _CCY
            and base not in SPECS)


def spec(symbol: str) -> dict:
    base = base_symbol((symbol or "").upper())
    s = SPECS.get(base)
    if s is None and _is_fx(base):
        s = {"contract_size": FX_CONTRACT_UNITS}
    if s is None:
        s = {"contract_size": 1.0}
    return {"symbol": base, "contract_size": s["contract_size"],
            "pip_size": pip_size(base), "min_lot": 0.01, "lot_step": 0.01}


def notional_usd(symbol: str, lot: float, price: float) -> float:
    base = base_symbol((symbol or "").upper())
    lot = float(lot or 0)
    price = float(price or 0)
    if lot <= 0 or price <= 0:
        return 0.0
    if base in SPECS:
        return lot * SPECS[base]["contract_size"] * price
    if _is_fx(base):
        units = lot * FX_CONTRACT_UNITS
        if base.startswith("USD"):
            return units                      # base currency IS USD
        if base.endswith("USD"):
            return units * price              # USD quote — exact
        # Fix plan B1/R1 — cross (EURJPY, EURGBP…): the position is `units` of the
        # BASE currency; convert that to USD (≈ table). `units × price` was the
        # notional in the QUOTE currency — ≈150× too high for JPY crosses.
        from pip_utils import QUOTE_USD_APPROX
        return units * QUOTE_USD_APPROX.get(base[:3], 1.0)
    return lot * price
