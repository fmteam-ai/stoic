"""Instrument spec service — ONE source of contract truth for risk math.

Before this module the platform carried several hard-coded copies of
contract specs (`lot × price × (100 if XAU else 1)`, the static pip-value
table, a 0.01 volume step everywhere). This service resolves a full
:class:`InstrumentSpec` per (account, symbol):

  1. **EA-reported** (``source="ea"``) — EA v1.54+ heartbeats carry
     ``symbol_specs`` (point, digits, stops/freeze level, trade_mode,
     tick_size, tick_value, contract_size, volume min/max/step) which
     ``routes/bridge_routes.py`` persists on the account document as
     ``accounts.symbol_specs[<BASE_SYMBOL>]`` (+ ``symbol_specs_updated_at``).
     Broker truth wins whenever present.
  2. **Static** (``source="static"``) — the existing tables in
     ``pip_utils`` / ``instruments`` (exactly today's numbers).

Fields the EA does NOT report today (coordinator follow-up, EA + bridge
change): ``SYMBOL_CURRENCY_BASE`` / ``SYMBOL_CURRENCY_PROFIT`` /
``SYMBOL_CURRENCY_MARGIN``. Until they arrive the currencies are inferred
from the symbol (FX pair parse, metals/crypto = USD quote, index table).
Storage contract for them (same dict, same key):
``accounts.symbol_specs[BASE] = {..., "currency_base": "EUR",
"currency_profit": "USD", "currency_margin": "EUR"}``.

FX conversion: :func:`usd_per_unit` returns USD per 1 unit of a currency
from the freshest stored tick (``price_ticks``), then a live
``market.get_quote``, then a static table.

Pure helpers (no I/O): :func:`pip_value_usd`, :func:`notional_usd`,
:func:`spec_from_account`, :func:`static_spec`.

Units:
  * ``tick_value`` for an EA spec is in the ACCOUNT DEPOSIT currency
    (MT5 SYMBOL_TRADE_TICK_VALUE semantics) — recorded in
    ``tick_value_currency``.
  * ``fx`` arguments are "USD per 1 unit" of the currency the value is
    natively expressed in (deposit ccy for EA tick values, profit ccy for
    static specs).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from pip_utils import (ACCOUNT_TYPE_LOT_MULTIPLIER, base_symbol, pip_size,
                       pip_value_usd_per_lot)

logger = logging.getLogger("instrument_specs")

DEFAULT_VOLUME_MIN = 0.01
DEFAULT_VOLUME_STEP = 0.01
DEFAULT_VOLUME_MAX = 100.0

# Static USD-per-unit fallbacks (only when no stored/live quote exists).
# JPY matches the 6.50 $/pip/lot assumption of the static pip table.
STATIC_USD_PER_UNIT = {
    "USD": 1.0, "USC": 0.01, "EUR": 1.08, "GBP": 1.27, "JPY": 0.0065,
    "CHF": 1.12, "AUD": 0.66, "NZD": 0.60, "CAD": 0.73, "SGD": 0.74,
    "HKD": 0.128, "CNH": 0.14, "SEK": 0.095, "NOK": 0.094, "DKK": 0.145,
    "PLN": 0.25, "ZAR": 0.054, "MXN": 0.055, "TRY": 0.03,
    "XAU": 2400.0, "XAG": 28.0, "BTC": 60000.0, "ETH": 3000.0,
}
# A stored tick older than this is not "latest" for FX conversion.
FX_TICK_MAX_AGE = timedelta(hours=6)

_FX = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SGD",
       "SEK", "NOK", "ZAR", "MXN", "PLN", "TRY", "HKD", "CNH", "DKK"}
# Index / commodity CFDs: quote (profit) currency.
_INDEX_CCY = {
    "US30": "USD", "NAS100": "USD", "SPX500": "USD", "US500": "USD",
    "USOIL": "USD", "GER40": "EUR", "UK100": "GBP", "JP225": "JPY",
    "FRA40": "EUR", "EU50": "EUR", "AUS200": "AUD", "HK50": "HKD",
}


@dataclass
class InstrumentSpec:
    symbol: str
    contract_size: float
    tick_size: float
    tick_value: float            # per 1.00 lot per tick, in tick_value_currency
    point: float
    digits: int
    volume_min: float = DEFAULT_VOLUME_MIN
    volume_step: float = DEFAULT_VOLUME_STEP
    volume_max: float = DEFAULT_VOLUME_MAX
    stops_level: float = 0.0     # points
    freeze_level: float = 0.0    # points
    quote_currency: str = "USD"
    base_currency: str = ""
    profit_currency: str = "USD"
    tick_value_currency: str = "USD"
    pip_size: float = 0.0001
    source: str = "static"       # "ea" | "static"
    trade_mode: Optional[int] = None
    stale: bool = False
    updated_at: Optional[str] = None
    extra: dict = field(default_factory=dict)

    @property
    def from_broker(self) -> bool:
        return self.source == "ea"

    def to_dict(self) -> dict:
        return asdict(self)


# ── currency inference ─────────────────────────────────────────────────
def infer_currencies(symbol: str) -> tuple[str, str]:
    """(base, profit/quote) currency for a STOIC base symbol."""
    b = base_symbol(symbol)
    p = b[:6]
    if len(p) == 6 and p[:3] in _FX and p[3:] in _FX and p[:3] != p[3:]:
        return p[:3], p[3:]
    if b in _INDEX_CCY:
        return "", _INDEX_CCY[b]
    if len(b) >= 6 and b.endswith("USD"):          # XAUUSD, BTCUSD, ETHUSD
        return b[:-3], "USD"
    return "", "USD"


def _f(v, default=0.0) -> float:
    try:
        out = float(v)
    except (TypeError, ValueError):
        return default
    return out if out == out else default  # NaN guard


# ── static fallback ────────────────────────────────────────────────────
def static_spec(symbol: str) -> InstrumentSpec:
    """Today's static contract tables, expressed as an InstrumentSpec.

    tick_size = pip size; tick_value = the static $/pip/standard-lot table
    value (so ``pip_value_usd`` reproduces the legacy numbers exactly)."""
    from instruments import spec as _legacy_spec
    b = base_symbol(symbol)
    leg = _legacy_spec(b)
    ps = pip_size(b)
    base_ccy, profit_ccy = infer_currencies(b)
    return InstrumentSpec(
        symbol=b, contract_size=float(leg["contract_size"]),
        tick_size=ps, tick_value=pip_value_usd_per_lot(b, "standard"),
        point=ps / 10.0, digits=0,
        volume_min=float(leg["min_lot"]), volume_step=float(leg["lot_step"]),
        volume_max=DEFAULT_VOLUME_MAX,
        quote_currency=profit_ccy, base_currency=base_ccy,
        profit_currency=profit_ccy, tick_value_currency="USD",
        pip_size=ps, source="static")


def _deposit_currency(account: dict | None) -> str:
    a = account or {}
    return str(a.get("broker_currency_reported") or a.get("currency")
               or "USD").upper()


def spec_from_account(account: dict | None, symbol: str,
                      max_age_sec: float | None = None) -> InstrumentSpec:
    """Sync resolver for callers that already hold the account document.

    EA-reported specs are used when the account carries a usable entry
    (point > 0 AND contract/tick data present); otherwise the static spec.
    ``max_age_sec`` only flags ``stale`` — contract specs rarely change, so
    staleness never silently swaps to a guessed table."""
    b = base_symbol(symbol)
    specs = (account or {}).get("symbol_specs") or {}
    raw = None
    if isinstance(specs, dict):
        raw = specs.get(b) or specs.get(str(symbol or "").upper())
    if not isinstance(raw, dict) or _f(raw.get("point")) <= 0:
        return static_spec(b)
    st = static_spec(b)
    contract = _f(raw.get("contract_size"))
    tick_size = _f(raw.get("tick_size"))
    tick_value = _f(raw.get("tick_value"))
    vmin = _f(raw.get("volume_min")) or st.volume_min
    vstep = _f(raw.get("volume_step")) or st.volume_step
    vmax = _f(raw.get("volume_max")) or st.volume_max
    base_ccy = str(raw.get("currency_base") or st.base_currency).upper()
    profit_ccy = str(raw.get("currency_profit") or st.profit_currency).upper()
    has_contract = contract > 0 and tick_size > 0 and tick_value > 0
    updated = (account or {}).get("symbol_specs_updated_at")
    stale = False
    if max_age_sec is not None and updated:
        try:
            ts = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            stale = ((datetime.now(timezone.utc) - ts).total_seconds()
                     > max_age_sec)
        except ValueError:
            stale = True
    return InstrumentSpec(
        symbol=b,
        contract_size=contract if contract > 0 else st.contract_size,
        tick_size=tick_size if has_contract else st.tick_size,
        tick_value=tick_value if has_contract else st.tick_value,
        tick_value_currency=(_deposit_currency(account) if has_contract
                             else "USD"),
        point=_f(raw.get("point")), digits=int(_f(raw.get("digits"))),
        volume_min=vmin, volume_step=vstep, volume_max=vmax,
        stops_level=_f(raw.get("stops_level_points")),
        freeze_level=_f(raw.get("freeze_level_points")),
        quote_currency=profit_ccy, base_currency=base_ccy,
        profit_currency=profit_ccy, pip_size=pip_size(b),
        # "ea" only when the contract economics came from the broker; a
        # pre-v1.54 EA (stop levels only) still yields static economics.
        source="ea" if has_contract else "static",
        trade_mode=(int(raw["trade_mode"])
                    if raw.get("trade_mode") is not None else None),
        stale=stale, updated_at=str(updated) if updated else None,
        extra={"volume_from_broker": _f(raw.get("volume_step")) > 0
               or _f(raw.get("volume_min")) > 0})


async def _load_account(db, account_id) -> dict | None:
    proj = {"symbol_specs": 1, "symbol_specs_updated_at": 1,
            "broker_currency_reported": 1, "currency": 1, "account_type": 1}
    if account_id is None:
        return None
    try:
        from bson import ObjectId
        oid = ObjectId(str(account_id))
    except Exception:  # noqa: BLE001 — non-ObjectId ids (tests, crypto)
        oid = None
    acc = None
    if oid is not None:
        acc = await db.accounts.find_one({"_id": oid}, proj)
    if acc is None:
        acc = await db.accounts.find_one({"_id": str(account_id)}, proj)
    return acc


async def get_spec(db, account_id, symbol: str, *,
                   account: dict | None = None) -> InstrumentSpec:
    """Resolve the spec for (account, symbol): EA-reported when available,
    static tables otherwise. Never raises for lookup failures — a DB error
    degrades to the static spec (which reproduces today's behaviour)."""
    acc = account
    if acc is None:
        try:
            acc = await _load_account(db, account_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("get_spec: account lookup failed (%s) — static "
                           "spec for %s", e, symbol)
            acc = None
    return spec_from_account(acc, symbol)


# ── FX conversion ──────────────────────────────────────────────────────
def static_usd_per_unit(currency: str) -> Optional[float]:
    return STATIC_USD_PER_UNIT.get(str(currency or "").upper())


async def _latest_tick_price(db, symbol: str) -> Optional[float]:
    try:
        tick = await db.price_ticks.find_one({"symbol": symbol},
                                             sort=[("ts", -1)])
    except Exception:  # noqa: BLE001
        return None
    if not tick:
        return None
    px = _f(tick.get("price"))
    ts = tick.get("ts")
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - ts > FX_TICK_MAX_AGE:
            return None
    return px if px > 0 else None


async def _live_quote_price(symbol: str) -> Optional[float]:
    try:
        from market import SYMBOL_MAP, get_quote
        if symbol not in SYMBOL_MAP:
            return None
        q = await get_quote(symbol)
        px = _f((q or {}).get("price"))
        return px if px > 0 else None
    except Exception:  # noqa: BLE001
        return None


async def usd_per_unit(db, currency: str, *, allow_network: bool = True
                       ) -> float:
    """USD value of 1 unit of ``currency``. Stored ticks first (no network),
    then a live quote, then the static table. Returns 0.0 only for an
    unknown currency with no quote at all (callers must treat that as
    'unknown' and fail closed)."""
    c = str(currency or "").upper()
    if c in ("USD", ""):
        return 1.0
    if c == "USC":
        return 0.01
    direct, inverse = f"{c}USD", f"USD{c}"
    for sym, inv in ((direct, False), (inverse, True)):
        px = await _latest_tick_price(db, sym) if db is not None else None
        if px:
            return 1.0 / px if inv else px
    if allow_network:
        for sym, inv in ((direct, False), (inverse, True)):
            px = await _live_quote_price(sym)
            if px:
                return 1.0 / px if inv else px
    return static_usd_per_unit(c) or 0.0


async def fx_for_spec(db, spec: InstrumentSpec, *,
                      allow_network: bool = True) -> float:
    """The ``fx`` to pass to the pure helpers for this spec."""
    ccy = (spec.tick_value_currency if spec.from_broker
           else spec.profit_currency)
    return await usd_per_unit(db, ccy, allow_network=allow_network)


# ── pure money helpers ─────────────────────────────────────────────────
def pip_value_per_lot_native(spec: InstrumentSpec) -> float:
    """Pip value per 1.00 lot in ``tick_value_currency`` (EA) — the broker's
    own tick value scaled from tick size to the STOIC pip size."""
    if spec.tick_size <= 0:
        return 0.0
    return spec.tick_value * (spec.pip_size / spec.tick_size)


def pip_value_usd(spec: InstrumentSpec, lots: float, price: float | None = None,
                  fx: float | None = None) -> float:
    """USD value of a 1-pip move on ``lots`` lots.

    EA spec : tick_value (deposit ccy) × pip/tick × lots × fx(deposit→USD).
    Static  : identical to ``pip_utils.pip_value_usd_per_lot(..., price)``
              (legacy numbers) unless ``fx`` (USD per profit-ccy unit) is
              given for a non-USD-profit instrument, in which case the exact
              contract × pip × fx is used (crosses, JPY pairs, EUR indices).
    """
    lots = _f(lots)
    if lots <= 0:
        return 0.0
    if spec.from_broker:
        conv = 1.0 if spec.tick_value_currency == "USD" else _f(fx)
        if conv <= 0:
            conv = static_usd_per_unit(spec.tick_value_currency) or 0.0
        return pip_value_per_lot_native(spec) * conv * lots
    if fx and _f(fx) > 0 and spec.profit_currency != "USD":
        return spec.contract_size * spec.pip_size * _f(fx) * lots
    return pip_value_usd_per_lot(spec.symbol, "standard", price=price) * lots


def notional_usd(spec: InstrumentSpec, lots: float, price: float,
                 fx: float | None = None) -> float:
    """USD notional of ``lots`` at ``price``.

    Static specs without ``fx`` reproduce ``instruments.notional_usd``
    exactly. EA specs use the broker contract size. A USD-base instrument
    (USDJPY) is ``lots × contract`` (the base IS USD); otherwise
    ``lots × contract × price`` in profit currency × fx (USD per profit-ccy
    unit; 1.0 for USD-quoted, static table when not supplied)."""
    lots, price = _f(lots), _f(price)
    if lots <= 0 or price <= 0:
        return 0.0
    if not spec.from_broker and not fx:
        from instruments import notional_usd as _legacy
        return _legacy(spec.symbol, lots, price)
    units = lots * spec.contract_size
    if spec.base_currency == "USD":
        return units
    if spec.profit_currency == "USD":
        return units * price
    conv = _f(fx) if fx else 0.0
    if conv <= 0:
        conv = static_usd_per_unit(spec.profit_currency) or 1.0
    return units * price * conv


def account_lot_multiplier(account: dict | None) -> float:
    return ACCOUNT_TYPE_LOT_MULTIPLIER.get(
        str((account or {}).get("account_type") or "standard").lower(), 1.0)


def sizing_pip_value_usd(account: dict | None, spec: InstrumentSpec,
                         price: float | None = None,
                         fx: float | None = None) -> Optional[float]:
    """Per-lot USD pip value for SIZING from a broker spec, or None when the
    broker value must not be used (static spec, cent/microcent account —
    whose deposit unit semantics are not certified — or a non-USD deposit
    currency without an FX rate). None means: use the legacy static path."""
    if not spec.from_broker:
        return None
    atype = str((account or {}).get("account_type") or "standard").lower()
    if atype in ("cent", "microcent"):
        return None
    if spec.tick_value_currency != "USD" and not (fx and _f(fx) > 0):
        return None
    v = pip_value_usd(spec, 1.0, price, fx)
    return v if v > 0 else None
