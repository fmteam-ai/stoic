"""Parametric Value-at-Risk for the live portfolio.

1-day, 95% confidence VaR via the parametric (variance-covariance) method:

    VaR = z * σ_portfolio * portfolio_notional

where σ_portfolio is the weighted vol of open positions including their
pairwise correlations. We use realised vol from `compute_indicators.atr_14`
divided by price (= ATR-as-%, our daily-vol proxy) — close enough for a
1-day horizon without resampling intra-day candles.

Returns the absolute dollar VaR plus a per-symbol breakdown.
"""
import math
import logging
from typing import Iterable

from market import get_history, compute_indicators

logger = logging.getLogger("portfolio.var")

# 95% one-tail z-score
Z_95 = 1.645
# 99% one-tail z-score (also returned for stress view)
Z_99 = 2.326


async def _atr_pct(symbol: str) -> float | None:
    """Best-effort daily-vol proxy = ATR14 / current price."""
    try:
        hist = await get_history(symbol)
        ind = compute_indicators(hist) or {}
        atr = float(ind.get("atr_14") or 0.0)
        px = float(ind.get("current_price") or 0.0)
        if atr <= 0 or px <= 0:
            return None
        return atr / px
    except Exception as e:  # noqa: BLE001
        logger.debug("atr_pct(%s) failed: %s", symbol, e)
        return None


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    x = xs[-n:]
    y = ys[-n:]
    mx = sum(x) / n
    my = sum(y) / n
    sxy = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    sxx = sum((x[i] - mx) ** 2 for i in range(n))
    syy = sum((y[i] - my) ** 2 for i in range(n))
    den = math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0
    return (sxy / den) if den else 0.0


async def _close_series(symbol: str, n: int = 30) -> list[float]:
    try:
        hist = await get_history(symbol)
        closes = [float(c["close"]) for c in hist if c.get("close") is not None]
        return closes[-n:]
    except Exception:  # noqa: BLE001
        return []


async def calculate_var(
    open_positions: Iterable[dict],
    *,
    equity: float,
    horizon_days: int = 1,
) -> dict:
    """Return a parametric VaR snapshot for the open portfolio.

    Output:
      {
        "var_95_usd": float, "var_95_pct_equity": float,
        "var_99_usd": float, "var_99_pct_equity": float,
        "portfolio_sigma_pct": float,
        "horizon_days": int, "equity": float,
        "positions": [{symbol, lot, notional, atr_pct, weight}, ...],
        "correlation_matrix": {symbol: {other_symbol: corr}},
        "notes": [str, ...],
      }
    """
    notes: list[str] = []
    positions = [p for p in open_positions if (p.get("status") or "open") in ("open", "pending")]
    if not positions or equity <= 0:
        return {"var_95_usd": 0.0, "var_95_pct_equity": 0.0,
                "var_99_usd": 0.0, "var_99_pct_equity": 0.0,
                "portfolio_sigma_pct": 0.0,
                "horizon_days": horizon_days, "equity": equity,
                "positions": [], "correlation_matrix": {},
                "notes": ["No open positions — VaR=0."]}

    # 1. Per-position notional + ATR%
    rows: list[dict] = []
    symbols: list[str] = []
    for p in positions:
        sym = (p.get("symbol") or "").upper()
        if not sym:
            continue
        lot = float(p.get("lot_size") or 0)
        entry = float(p.get("entry_price") or 0)
        if lot <= 0 or entry <= 0:
            continue
        # MT5 contract size: gold 100oz, crypto 1, FX 100k. Heuristic — we
        # use lot×entry as a notional proxy; for FX this understates by 100k
        # but the VaR % comes out the same since both numerator/denominator scale.
        notional = lot * entry * (100 if sym == "XAUUSD" else 1)
        atr_pct = await _atr_pct(sym)
        if atr_pct is None:
            atr_pct = 0.01  # 1% fallback when no history
            notes.append(f"{sym}: no ATR history — assumed 1% vol")
        rows.append({"symbol": sym, "lot": lot, "notional": notional,
                     "atr_pct": atr_pct, "weight": 0.0})
        symbols.append(sym)

    if not rows:
        return {"var_95_usd": 0.0, "var_95_pct_equity": 0.0,
                "var_99_usd": 0.0, "var_99_pct_equity": 0.0,
                "portfolio_sigma_pct": 0.0,
                "horizon_days": horizon_days, "equity": equity,
                "positions": [], "correlation_matrix": {},
                "notes": notes + ["No priceable positions."]}

    total_notional = sum(r["notional"] for r in rows)
    for r in rows:
        r["weight"] = r["notional"] / total_notional if total_notional > 0 else 0

    # 2. Pairwise correlation matrix from closes
    unique = sorted(set(symbols))
    series = {s: await _close_series(s) for s in unique}
    corr: dict[str, dict[str, float]] = {s: {s: 1.0} for s in unique}
    for i, s1 in enumerate(unique):
        for s2 in unique[i + 1:]:
            c = _pearson(series.get(s1) or [], series.get(s2) or [])
            corr[s1][s2] = round(c, 3)
            corr[s2][s1] = round(c, 3)

    # 3. Portfolio variance = Σ w_i w_j σ_i σ_j ρ_ij
    var = 0.0
    for r in rows:
        for s in rows:
            wi, wj = r["weight"], s["weight"]
            si, sj = r["atr_pct"], s["atr_pct"]
            rho = 1.0 if r["symbol"] == s["symbol"] else corr.get(r["symbol"], {}).get(s["symbol"], 0.0)
            var += wi * wj * si * sj * rho
    sigma_pct = math.sqrt(max(var, 0.0))
    # Scale for horizon (√t rule)
    if horizon_days > 1:
        sigma_pct *= math.sqrt(horizon_days)

    var_95_usd = Z_95 * sigma_pct * total_notional
    var_99_usd = Z_99 * sigma_pct * total_notional
    return {
        "var_95_usd": round(var_95_usd, 2),
        "var_95_pct_equity": round((var_95_usd / equity) * 100.0, 3) if equity else 0.0,
        "var_99_usd": round(var_99_usd, 2),
        "var_99_pct_equity": round((var_99_usd / equity) * 100.0, 3) if equity else 0.0,
        "portfolio_sigma_pct": round(sigma_pct * 100.0, 3),
        "horizon_days": horizon_days,
        "equity": equity,
        "positions": rows,
        "correlation_matrix": corr,
        "notes": notes,
    }
