"""Funding-rate carry (cash-and-carry) — scanner + SHADOW book.

The trade: buy the coin on spot and short the same quantity on the USDT
perpetual. Price moves cancel between the legs; while the perp funding rate
is positive, longs pay shorts every funding interval, so the hedged book
collects that payment.

Scope (v1) is deliberately SHADOW ONLY:
  • the scanner reads PUBLIC market data (no API keys, no orders);
  • the shadow book paper-trades the hedged pair in `funding_carry_shadow`
    so the strategy builds a track record before any capital is involved;
  • there is NO live execution path here. Going live needs a two-leg
    execution engine that clears the capability matrix, reservations and the
    staged rollout like every other engine (see crypto_bridge.ccxt_engine).

The risks this models (the carry is NOT risk-free):
  • funding flips negative  → the short starts paying; exit on a rolling mean;
  • short-perp liquidation  → price spikes eat the perp margin; liquidation
                              distance is checked every cycle;
  • basis                   → entering with the perp at a discount is a cost;
  • hedge drift             → legs of different size are no longer neutral;
  • fees                    → four taker legs per round trip must be repaid
                              by funding before the trade is worth taking.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("crypto.funding_carry")

COLLECTION = "funding_carry_shadow"
# Venues whose unified CCXT client exposes both spot and USDT-margined perps.
CARRY_EXCHANGES = ("binance", "okx")
DEFAULT_UNIVERSE = ("BTC", "ETH", "SOL")


@dataclass
class CarryConfig:
    # entry
    min_net_apr: float = 0.08              # 8% net of fees over the horizon
    min_positive_ratio: float = 0.75       # share of recent fundings that were > 0
    max_abs_basis: float = 0.005           # |perp/spot - 1| above this → skip (dislocated)
    horizon_days: float = 14.0             # holding period the fees are amortised over
    history_periods: int = 21              # ~7 days of 8h fundings
    # exit
    exit_window: int = 3                   # rolling mean over the last N fundings…
    exit_mean_rate: float = 0.0            # …at or below this closes the trade
    exit_abs_basis: float = 0.02           # basis blow-out closes the trade
    # risk
    leverage: float = 2.0                  # perp leverage (isolated margin)
    maint_margin_rate: float = 0.004       # exchange maintenance margin rate
    min_liq_distance: float = 0.25         # warn below 25% move to liquidation
    hedge_tolerance: float = 0.02          # |spot qty / perp qty - 1| above this → rebalance
    # costs (fractions of notional)
    spot_fee: float = 0.001
    perp_fee: float = 0.0005
    slippage: float = 0.0005               # per leg
    # capital (shadow)
    capital: float = 10_000.0
    max_position_notional: float = 2_500.0
    max_positions: int = 3

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "CarryConfig":
        """Build from user overrides; unknown keys are ignored, values clamped to sane bounds."""
        base = cls()
        out = {}
        for k, v in (d or {}).items():
            if k not in cls.__dataclass_fields__:
                continue
            v = type(getattr(base, k))(v)
            lo, hi = _BOUNDS.get(k, (0.0, None))
            v = max(lo, v) if hi is None else min(max(lo, v), hi)
            out[k] = type(getattr(base, k))(v)
        return cls(**out)


_BOUNDS = {
    "horizon_days": (1.0, 365.0), "history_periods": (3, 200), "exit_window": (1, 50),
    "leverage": (1.0, 10.0), "min_positive_ratio": (0.0, 1.0), "max_positions": (1, 10),
    "capital": (0.0, 1e9), "max_position_notional": (0.0, 1e9), "maint_margin_rate": (0.0, 0.1),
    "exit_mean_rate": (-1.0, 1.0), "min_net_apr": (-10.0, 10.0),
}


@dataclass
class FundingSnapshot:
    base: str
    exchange_id: str
    spot_symbol: str
    perp_symbol: str
    spot_price: float
    perp_price: float
    funding_rate: float                    # current / next predicted rate
    interval_hours: float = 8.0
    history: list = field(default_factory=list)   # [{"ts": ms, "rate": float}], oldest → newest
    next_funding_ts: Optional[int] = None
    fetched_at: Optional[str] = None


# ───────────────────────────── pure maths ─────────────────────────────

def periods_per_year(interval_hours: float) -> float:
    return 24.0 / max(interval_hours, 1e-9) * 365.0


def annualize(rate: float, interval_hours: float = 8.0) -> float:
    return rate * periods_per_year(interval_hours)


def basis(spot_price: float, perp_price: float) -> float:
    """Perp premium over spot (+ = perp rich, the short side gains on convergence)."""
    return perp_price / spot_price - 1.0


def round_trip_cost(cfg: CarryConfig) -> float:
    """Open + close both legs: 2 × (spot taker + perp taker + 2 × slippage)."""
    return 2.0 * (cfg.spot_fee + cfg.perp_fee + 2.0 * cfg.slippage)


def short_liquidation_price(entry: float, leverage: float, mmr: float) -> float:
    """Isolated USDT-margined short: liquidated when losses eat the margin down to maintenance."""
    return entry * (1.0 + 1.0 / leverage) / (1.0 + mmr)


def liquidation_distance(mark: float, liq_price: float) -> float:
    """Fractional price rise left before the short perp is liquidated."""
    return liq_price / mark - 1.0


def capital_required(notional: float, leverage: float) -> float:
    """Spot leg is fully funded; the perp leg only posts margin."""
    return notional * (1.0 + 1.0 / leverage)


def evaluate(snap: FundingSnapshot, cfg: CarryConfig) -> dict:
    """Score one market for entry. Never raises; reasons explain every skip."""
    rates = [float(h["rate"]) for h in snap.history[-cfg.history_periods:]]
    mean_rate = sum(rates) / len(rates) if rates else snap.funding_rate
    positive_ratio = (sum(1 for r in rates if r > 0) / len(rates)) if rates else 0.0
    b = basis(snap.spot_price, snap.perp_price)

    per_day = mean_rate * 24.0 / snap.interval_hours
    # Conservative: a perp discount (b < 0) is a cost, a premium is NOT counted as gain.
    cost = round_trip_cost(cfg) + max(-b, 0.0)
    net_apr = (per_day * cfg.horizon_days - cost) / cfg.horizon_days * 365.0
    breakeven_days = cost / per_day if per_day > 0 else None

    reasons = []
    if not rates:
        reasons.append("no_funding_history")
    if snap.funding_rate <= 0:
        reasons.append("current_funding_not_positive")
    if positive_ratio < cfg.min_positive_ratio:
        reasons.append("funding_not_persistent")
    if abs(b) > cfg.max_abs_basis:
        reasons.append("basis_dislocated")
    if net_apr < cfg.min_net_apr:
        reasons.append("net_apr_below_min")
    if breakeven_days is None or breakeven_days > cfg.horizon_days:
        reasons.append("breakeven_beyond_horizon")

    return {
        "base": snap.base, "exchange_id": snap.exchange_id,
        "spot_symbol": snap.spot_symbol, "perp_symbol": snap.perp_symbol,
        "spot_price": snap.spot_price, "perp_price": snap.perp_price,
        "funding_rate": snap.funding_rate,
        "funding_apr": annualize(snap.funding_rate, snap.interval_hours),
        "mean_rate": mean_rate, "mean_apr": annualize(mean_rate, snap.interval_hours),
        "positive_ratio": round(positive_ratio, 4), "periods": len(rates),
        "basis": b, "round_trip_cost": cost, "net_apr": net_apr,
        "breakeven_days": breakeven_days,
        "liquidation_distance": liquidation_distance(
            snap.perp_price, short_liquidation_price(snap.perp_price, cfg.leverage, cfg.maint_margin_rate)),
        "eligible": not reasons, "reasons": reasons,
    }


def assess(position: dict, snap: FundingSnapshot, cfg: CarryConfig) -> dict:
    """Decide hold / rebalance / exit for an open hedged position."""
    mark = snap.perp_price
    liq = position.get("liq_price") or short_liquidation_price(
        position["entry_perp"], position.get("leverage", cfg.leverage), cfg.maint_margin_rate)
    dist = liquidation_distance(mark, liq)
    window = [float(h["rate"]) for h in snap.history[-cfg.exit_window:]]
    rolling = sum(window) / len(window) if window else snap.funding_rate
    b = basis(snap.spot_price, snap.perp_price)
    drift = position["spot_qty"] / position["perp_qty"] - 1.0 if position.get("perp_qty") else float("inf")

    if mark >= liq:
        action, reason = "liquidated", "perp_liquidated"
    elif dist < cfg.min_liq_distance / 2:
        action, reason = "exit", "liquidation_risk_critical"
    elif len(window) >= cfg.exit_window and rolling <= cfg.exit_mean_rate:
        action, reason = "exit", "funding_turned"
    elif abs(b) > cfg.exit_abs_basis:
        action, reason = "exit", "basis_blowout"
    elif abs(drift) > cfg.hedge_tolerance:
        action, reason = "rebalance", "hedge_drift"
    elif dist < cfg.min_liq_distance:
        action, reason = "add_margin", "liquidation_distance_low"
    else:
        action, reason = "hold", None
    return {"action": action, "reason": reason, "liq_price": liq, "liquidation_distance": dist,
            "rolling_rate": rolling, "basis": b, "hedge_drift": drift}


def mark_to_market(position: dict, snap: FundingSnapshot) -> dict:
    spot_pnl = position["spot_qty"] * (snap.spot_price - position["entry_spot"])
    perp_pnl = position["perp_qty"] * (position["entry_perp"] - snap.perp_price)
    funding = position.get("funding_accrued", 0.0)
    fees = position.get("fees_paid", 0.0)
    return {"spot_pnl": spot_pnl, "perp_pnl": perp_pnl, "funding": funding, "fees": fees,
            "net_pnl": spot_pnl + perp_pnl + funding - fees}


def new_settlements(position: dict, snap: FundingSnapshot) -> list:
    """Funding settlements that landed after the position opened and were not yet accrued."""
    since = max(int(position.get("last_funding_ts") or 0), int(position.get("opened_ts") or 0))
    return [h for h in snap.history if int(h["ts"]) > since]


def leg_fees(notional: float, cfg: CarryConfig) -> float:
    """One side (open OR close) of both legs, including slippage."""
    return notional * (cfg.spot_fee + cfg.perp_fee + 2.0 * cfg.slippage)


# ─────────────────────────── public scanner ───────────────────────────

_SCAN_CACHE: dict = {}
_SCAN_TTL_SEC = 60


def _public_exchange(exchange_id: str):
    import ccxt.async_support as ccxt  # type: ignore[import]
    if exchange_id not in CARRY_EXCHANGES:
        raise ValueError(f"funding carry supports: {', '.join(CARRY_EXCHANGES)}")
    # No keys: public endpoints only. This client can never place an order.
    return getattr(ccxt, exchange_id)({"enableRateLimit": True})


def _interval_hours(info: dict) -> float:
    raw = str(info.get("interval") or "8h").lower().rstrip("h")
    try:
        return float(raw) or 8.0
    except ValueError:
        return 8.0


async def fetch_snapshot(ex, exchange_id: str, base: str, history_periods: int = 21) -> FundingSnapshot:
    spot_symbol, perp_symbol = f"{base}/USDT", f"{base}/USDT:USDT"
    fr, hist, spot = await asyncio.gather(
        ex.fetch_funding_rate(perp_symbol),
        ex.fetch_funding_rate_history(perp_symbol, None, history_periods),
        ex.fetch_ticker(spot_symbol),
    )
    perp_price = float(fr.get("markPrice") or fr.get("indexPrice") or 0)
    spot_price = float(spot.get("last") or spot.get("close") or 0)
    if perp_price <= 0 or spot_price <= 0:
        raise ValueError(f"{base}: missing price")
    history = sorted(({"ts": int(h["timestamp"]), "rate": float(h["fundingRate"])}
                      for h in hist if h.get("timestamp") is not None and h.get("fundingRate") is not None),
                     key=lambda h: h["ts"])
    return FundingSnapshot(
        base=base, exchange_id=exchange_id, spot_symbol=spot_symbol, perp_symbol=perp_symbol,
        spot_price=spot_price, perp_price=perp_price,
        funding_rate=float(fr.get("fundingRate") or 0.0), interval_hours=_interval_hours(fr),
        history=history, next_funding_ts=fr.get("fundingTimestamp") or fr.get("nextFundingTimestamp"),
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


async def scan(exchange_id: str = "binance", bases=DEFAULT_UNIVERSE, cfg: Optional[CarryConfig] = None,
               exchange=None) -> dict:
    """Fetch + evaluate each base. Errors are per-symbol and redacted, never raised."""
    from crypto_bridge.ccxt_engine import redact_exchange_error
    cfg = cfg or CarryConfig()
    key = (exchange_id, tuple(bases), cfg.history_periods)
    hit = _SCAN_CACHE.get(key)
    if exchange is None and hit and time.time() - hit[0] < _SCAN_TTL_SEC:
        snaps, errors = hit[1], hit[2]
    else:
        ex = exchange or _public_exchange(exchange_id)
        snaps, errors = {}, {}
        try:
            results = await asyncio.gather(
                *[fetch_snapshot(ex, exchange_id, b, cfg.history_periods) for b in bases], return_exceptions=True)
        finally:
            if exchange is None:
                await ex.close()
        for b, r in zip(bases, results):
            if isinstance(r, BaseException):
                errors[b] = redact_exchange_error(r, f"funding_carry:{exchange_id}:{b}")
            else:
                snaps[b] = r
        if exchange is None:
            _SCAN_CACHE[key] = (time.time(), snaps, errors)
    rows = sorted((evaluate(s, cfg) for s in snaps.values()), key=lambda r: r["net_apr"], reverse=True)
    return {"exchange_id": exchange_id, "mode": "shadow", "config": asdict(cfg),
            "opportunities": rows, "errors": errors, "snapshots": snaps}


# ───────────────────────────── shadow book ─────────────────────────────

def _now_ms() -> int:
    return int(time.time() * 1000)


async def run_shadow_cycle(db, user_id: str, scan_result: dict, cfg: Optional[CarryConfig] = None,
                           now_ms: Optional[int] = None) -> dict:
    """One paper-trading step: accrue funding, act on open positions, open new ones.

    Simplification (shadow only): funding is accrued on perp notional at the
    CURRENT mark, and add_margin / rebalance are recorded as alerts rather
    than simulated — a live engine must act on them.
    """
    cfg = cfg or CarryConfig()
    now_ms = now_ms or _now_ms()
    now_iso = datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat()
    snaps: dict = scan_result.get("snapshots") or {}
    coll = db[COLLECTION]
    events = []

    open_rows = await coll.find({"user_id": user_id, "status": "open"}).to_list(100)
    for pos in open_rows:
        snap = snaps.get(pos["base"])
        if snap is None or snap.exchange_id != pos["exchange_id"]:
            continue
        accrued, last_ts = 0.0, pos.get("last_funding_ts") or pos["opened_ts"]
        for h in new_settlements(pos, snap):
            accrued += h["rate"] * pos["perp_qty"] * snap.perp_price   # short receives when rate > 0
            last_ts = max(last_ts, int(h["ts"]))
        pos["funding_accrued"] = pos.get("funding_accrued", 0.0) + accrued
        pos["last_funding_ts"] = last_ts
        verdict = assess(pos, snap, cfg)
        upd = {"funding_accrued": pos["funding_accrued"], "last_funding_ts": last_ts,
               "last_assessment": verdict, "updated_at": now_iso}
        if verdict["action"] in ("exit", "liquidated"):
            if verdict["action"] == "liquidated":
                # Perp margin is gone; the spot leg is sold at market.
                margin = pos["perp_qty"] * pos["entry_perp"] / pos.get("leverage", cfg.leverage)
                pos["fees_paid"] = pos.get("fees_paid", 0.0) + pos["spot_qty"] * snap.spot_price * (cfg.spot_fee + cfg.slippage)
                spot_pnl = pos["spot_qty"] * (snap.spot_price - pos["entry_spot"])
                realized = spot_pnl - margin + pos["funding_accrued"] - pos["fees_paid"]
            else:
                pos["fees_paid"] = pos.get("fees_paid", 0.0) + leg_fees(pos["perp_qty"] * snap.perp_price, cfg)
                realized = mark_to_market(pos, snap)["net_pnl"]
            upd.update({"status": "closed" if verdict["action"] == "exit" else "liquidated",
                        "close_reason": verdict["reason"], "closed_at": now_iso,
                        "exit_spot": snap.spot_price, "exit_perp": snap.perp_price,
                        "fees_paid": pos["fees_paid"], "realized_pnl": realized})
        else:
            upd["unrealized"] = mark_to_market(pos, snap)
        await coll.update_one({"_id": pos["_id"]}, {"$set": upd})
        events.append({"base": pos["base"], "action": verdict["action"], "reason": verdict["reason"],
                       "funding_accrued_now": accrued})

    still_open = await coll.find({"user_id": user_id, "status": "open"}).to_list(100)
    held = {p["base"] for p in still_open}
    used = sum(capital_required(p["perp_qty"] * p["entry_perp"], p.get("leverage", cfg.leverage)) for p in still_open)
    for opp in scan_result.get("opportunities") or []:
        if len(held) >= cfg.max_positions:
            break
        if not opp["eligible"] or opp["base"] in held or opp["base"] not in snaps:
            continue
        notional = min(cfg.max_position_notional,
                       (cfg.capital - used) / (1.0 + 1.0 / cfg.leverage))
        if notional < 10.0:                    # exchange minimum notional
            break
        snap = snaps[opp["base"]]
        qty = notional / snap.spot_price
        doc = {"user_id": user_id, "mode": "shadow", "status": "open",
               "base": snap.base, "exchange_id": snap.exchange_id,
               "spot_symbol": snap.spot_symbol, "perp_symbol": snap.perp_symbol,
               "spot_qty": qty, "perp_qty": qty, "leverage": cfg.leverage,
               "entry_spot": snap.spot_price, "entry_perp": snap.perp_price,
               "liq_price": short_liquidation_price(snap.perp_price, cfg.leverage, cfg.maint_margin_rate),
               "fees_paid": leg_fees(notional, cfg), "funding_accrued": 0.0,
               "opened_ts": now_ms, "last_funding_ts": now_ms, "opened_at": now_iso,
               "entry_eval": {k: opp[k] for k in ("net_apr", "mean_apr", "positive_ratio", "basis", "breakeven_days")}}
        await coll.insert_one(doc)
        held.add(snap.base)
        used += capital_required(notional, cfg.leverage)
        events.append({"base": snap.base, "action": "open", "reason": None, "notional": notional})

    return {"mode": "shadow", "events": events, "summary": await shadow_summary(db, user_id)}


async def shadow_summary(db, user_id: str) -> dict:
    rows = await db[COLLECTION].find({"user_id": user_id}).sort("opened_ts", -1).to_list(500)
    open_rows = [r for r in rows if r.get("status") == "open"]
    closed = [r for r in rows if r.get("status") in ("closed", "liquidated")]
    return {
        "open_positions": len(open_rows),
        "closed_positions": len(closed),
        "liquidations": sum(1 for r in closed if r["status"] == "liquidated"),
        "realized_pnl": sum(r.get("realized_pnl", 0.0) for r in closed),
        "unrealized_pnl": sum((r.get("unrealized") or {}).get("net_pnl", 0.0) for r in open_rows),
        "funding_collected": sum(r.get("funding_accrued", 0.0) for r in rows),
        "fees_paid": sum(r.get("fees_paid", 0.0) for r in rows),
        "alerts": [{"base": r["base"], **{k: r["last_assessment"][k] for k in ("action", "reason")}}
                   for r in open_rows
                   if (r.get("last_assessment") or {}).get("action") in ("rebalance", "add_margin")],
    }
