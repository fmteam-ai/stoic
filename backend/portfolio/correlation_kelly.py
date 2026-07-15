"""Correlation-aware portfolio allocation + dynamic risk budgeting (iter-51).

Two defence-in-depth lot-size trims applied AFTER the per-account Kelly
sizing (`compute_lot_for_account`) and AFTER the Kelly-scaled max-lot cap
in `bot_runner`. Both can ONLY shrink the lot — never inflate it.

(A) **Correlation penalty** — when a proposed new trade is positively
    correlated with existing same-direction positions (or negatively
    correlated with opposite-direction positions, which collapses to the
    same risk concentration), trim lots so the marginal contribution to
    portfolio risk stays bounded.

    Mathematically:
        corr_pressure = Σ_i max(0, ρ_eff_i) × w_i
    where:
        w_i           = existing position i's notional / equity
        ρ_eff_i       = +ρ_i  when (new_action, existing_action) match
                        −ρ_i  when they oppose (a hedge → no penalty)
        corr_scale    = 1 / (1 + α × corr_pressure),  α defaults to 2.0

(B) **CVaR budget** — forecast the post-trade portfolio CVaR (parametric
    Expected-Shortfall under Normality) and, if it exceeds the target
    (default 2% of equity / day), trim further so:
        cvar_scale = target_cvar_pct / forecast_cvar_pct
    Always clamped to (MIN_SCALE, 1.0].

The two scales compose multiplicatively. The wire-up is **disabled by
default** behind `CORRELATION_KELLY_ENABLED` so iter-51 is a safe opt-in
ship; the helper itself is pure-deterministic and unit-tested.
"""
from __future__ import annotations
import logging
import math
import os
from typing import Iterable

from portfolio.var import _atr_pct, ES_MULT_95, UNKNOWN_RHO, corr_returns

logger = logging.getLogger("portfolio.correlation-kelly")

# Concentration aggressiveness — larger α = stronger trim on correlated stacks.
ALPHA = float(os.environ.get("CORRELATION_KELLY_ALPHA", "2.0"))
# Lower-bound on the combined scale; below this we'd rather skip the trade
# entirely than fire a meaningless dust-lot.
MIN_SCALE = float(os.environ.get("CORRELATION_KELLY_MIN_SCALE", "0.25"))
# Default CVaR target as % of equity (one-day, 95% expected-shortfall).
DEFAULT_CVAR_TARGET_PCT = float(os.environ.get("PORTFOLIO_CVAR_TARGET_PCT", "2.0"))


def _normalize_action(a: str | None) -> str:
    if not a:
        return ""
    s = str(a).strip().upper()
    if s.startswith("BUY") or s == "LONG":
        return "BUY"
    if s.startswith("SELL") or s == "SHORT":
        return "SELL"
    return s


def _notional_of(p: dict) -> float:
    # iter-144 C4 · canonical registry (FX = 100k units, metals/crypto CFDs)
    from instruments import notional_usd
    return notional_usd(p.get("symbol") or "", p.get("lot_size") or 0,
                        p.get("entry_price") or 0)


async def compute_correlation_aware_scale(
    *,
    new_symbol: str,
    new_action: str,
    new_notional: float,
    open_positions: Iterable[dict],
    equity: float,
    new_atr_pct: float | None = None,
    cvar_target_pct: float | None = None,
) -> dict:
    """Compute a trim factor in (0, 1.0] for a proposed new trade.

    Returns dict shape:
      {
        "scale": float,            # final multiplier applied to the proposed lot
        "reason": str,             # human-readable summary
        "corr_pressure": float,    # 0..N
        "corr_scale": float,
        "cvar_forecast_pct": float | None,
        "cvar_target_pct": float,
        "cvar_scale": float,
      }
    """
    new_sym = (new_symbol or "").upper()
    new_act = _normalize_action(new_action)
    cvar_target = float(cvar_target_pct if cvar_target_pct is not None else DEFAULT_CVAR_TARGET_PCT)

    # Filter to currently-active positions only — paper/shadow rows could be
    # passed in but the caller should pre-filter; we defend anyway.
    positions = [p for p in (open_positions or [])
                 if (p.get("status") or "open") in ("open", "pending")]

    base = {
        "scale": 1.0, "reason": "",
        "corr_pressure": 0.0, "corr_scale": 1.0,
        "cvar_forecast_pct": None, "cvar_target_pct": cvar_target, "cvar_scale": 1.0,
    }

    if not positions or new_notional <= 0 or equity <= 0:
        base["reason"] = "no open positions — no correlation/CVaR trim"
        return base

    # ── 1) Correlation penalty ────────────────────────────────────────────
    # iter-142 · Timestamp-aligned daily-return correlation; unknown pairs
    # (insufficient overlapping history) use the conservative UNKNOWN_RHO
    # prior instead of assuming independence.
    pressure = 0.0
    same_direction_corr_sum = 0.0  # for the reason string only
    pair_count = 0
    for p in positions:
        sym = (p.get("symbol") or "").upper()
        act = _normalize_action(p.get("action"))
        notion = _notional_of(p)
        if notion <= 0 or not sym:
            continue
        # Same-symbol stacking: treat ρ=1.0 (anti-pyramid path also catches
        # this, but defence-in-depth means we still penalize here).
        if sym == new_sym:
            rho = 1.0
        else:
            rho = await corr_returns(new_sym, sym)
            if rho is None:
                rho = UNKNOWN_RHO
        # Direction-aware effective correlation:
        #   matching actions → +ρ (same-direction stacking adds risk)
        #   opposing actions → −ρ (a real hedge — clip to zero, no penalty)
        if act and new_act and act == new_act:
            rho_eff = rho
        else:
            rho_eff = -rho
        if rho_eff <= 0:
            continue
        w_i = notion / equity if equity > 0 else 0.0
        pressure += rho_eff * w_i
        same_direction_corr_sum += rho_eff
        pair_count += 1

    corr_scale = 1.0 / (1.0 + ALPHA * pressure) if pressure > 0 else 1.0

    # ── 2) CVaR budget ────────────────────────────────────────────────────
    # Build a hypothetical "after-this-trade" portfolio at the *correlation-
    # trimmed* lot, then compute parametric portfolio CVaR. If it overshoots
    # the budget, shrink further so the forecast meets the target.
    new_lot_after_corr_notional = new_notional * corr_scale
    if new_lot_after_corr_notional <= 0:
        return {**base, "scale": MIN_SCALE,
                "reason": f"correlation-trim collapsed to dust — clamped at {MIN_SCALE:.2f}",
                "corr_pressure": round(pressure, 4),
                "corr_scale": round(corr_scale, 4)}

    # Assemble vol+notional rows for hypothetical book (existing + new).
    rows: list[dict] = []
    seen_syms: set[str] = set()
    for p in positions:
        sym = (p.get("symbol") or "").upper()
        notion = _notional_of(p)
        if notion <= 0 or not sym:
            continue
        atr = await _atr_pct(sym)
        rows.append({"symbol": sym, "notional": notion,
                     "atr_pct": atr if atr is not None else 0.01})
        seen_syms.add(sym)
    atr_new = new_atr_pct if new_atr_pct is not None else (await _atr_pct(new_sym))
    if atr_new is None:
        atr_new = 0.01
    rows.append({"symbol": new_sym, "notional": new_lot_after_corr_notional,
                 "atr_pct": atr_new})
    seen_syms.add(new_sym)

    total_notional = sum(r["notional"] for r in rows)
    if total_notional <= 0:
        return {**base, "scale": corr_scale,
                "reason": "no measurable notional — correlation-only trim",
                "corr_pressure": round(pressure, 4),
                "corr_scale": round(corr_scale, 4)}
    for r in rows:
        r["weight"] = r["notional"] / total_notional

    # Pairwise correlation matrix for hypothetical book (aligned returns;
    # unknown pairs → conservative UNKNOWN_RHO).
    unique = sorted(seen_syms)
    corr_mat: dict[str, dict[str, float]] = {s: {s: 1.0} for s in unique}
    for i, s1 in enumerate(unique):
        for s2 in unique[i + 1:]:
            c = await corr_returns(s1, s2)
            if c is None:
                c = UNKNOWN_RHO
            corr_mat[s1][s2] = c
            corr_mat[s2][s1] = c

    # Σ w_i w_j σ_i σ_j ρ_ij
    port_var = 0.0
    for ri in rows:
        for rj in rows:
            rho_ij = 1.0 if ri["symbol"] == rj["symbol"] else corr_mat[ri["symbol"]].get(rj["symbol"], 0.0)
            port_var += ri["weight"] * rj["weight"] * ri["atr_pct"] * rj["atr_pct"] * rho_ij
    sigma_pct = math.sqrt(max(port_var, 0.0))

    cvar_pct_equity = ES_MULT_95 * sigma_pct * total_notional / equity * 100.0
    if cvar_pct_equity > cvar_target and cvar_pct_equity > 0:
        cvar_scale = cvar_target / cvar_pct_equity
    else:
        cvar_scale = 1.0

    combined = corr_scale * cvar_scale
    if combined < MIN_SCALE:
        combined = MIN_SCALE
    if combined > 1.0:
        combined = 1.0

    bits: list[str] = []
    if corr_scale < 1.0:
        bits.append(f"corr_pressure={pressure:.3f} (×{pair_count} pos) → ×{corr_scale:.3f}")
    if cvar_scale < 1.0:
        bits.append(f"CVaR_95 forecast {cvar_pct_equity:.2f}% > target {cvar_target:.2f}% → ×{cvar_scale:.3f}")
    if not bits:
        reason = (f"within budget · CVaR_95 forecast {cvar_pct_equity:.2f}%/"
                  f"{cvar_target:.2f}% · no correlation pressure")
    else:
        reason = " · ".join(bits) + f" → combined ×{combined:.3f}"

    return {
        "scale": round(combined, 4),
        "reason": reason,
        "corr_pressure": round(pressure, 4),
        "corr_scale": round(corr_scale, 4),
        "cvar_forecast_pct": round(cvar_pct_equity, 3),
        "cvar_target_pct": cvar_target,
        "cvar_scale": round(cvar_scale, 4),
    }
