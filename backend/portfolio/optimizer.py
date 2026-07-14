"""iter-139 · Portfolio-level optimization report (institutional Phase B).

Computes correlation- and volatility-aware TARGET weights across the
symbols currently traded, compares them with the ACTUAL open exposure and
flags concentration / correlation / CVaR-budget breaches.

READ-ONLY report: the live per-trade enforcement is already handled by
portfolio/correlation_kelly.py (correlation trim + CVaR budget) — this
module gives the portfolio-level view the allocator and the UI consume.

Target weights: inverse-volatility, penalized by average positive pairwise
correlation:   raw_i = (1 / vol_i) × 1 / (1 + avg_pos_corr_i)
then normalized to sum to 1.
"""
import logging
import math

from portfolio.var import ES_MULT_95, UNKNOWN_RHO, _atr_pct, corr_returns
from portfolio.correlation_kelly import DEFAULT_CVAR_TARGET_PCT, _notional_of

logger = logging.getLogger("portfolio-optimizer")

CONCENTRATION_MAX_PCT = 60.0   # PROMOTION_CRITERIA: no symbol > 60%
CORR_WARN = 0.7


def target_weights(vols: dict, corr: dict) -> dict:
    """Pure: {sym: vol_pct}, {sym: {sym: rho}} → normalized target weights."""
    raw = {}
    syms = sorted(vols)
    for s in syms:
        v = max(float(vols[s] or 0), 1e-6)
        others = [max(corr.get(s, {}).get(o, 0.0), 0.0)
                  for o in syms if o != s]
        avg_pos_corr = sum(others) / len(others) if others else 0.0
        raw[s] = (1.0 / v) / (1.0 + avg_pos_corr)
    total = sum(raw.values()) or 1.0
    return {s: round(raw[s] / total, 4) for s in syms}


async def optimization_report(db, user_id: str) -> dict:
    open_trades = await db.trades.find(
        {"user_id": user_id, "status": {"$in": ["open", "pending"]}},
        {"symbol": 1, "action": 1, "lot_size": 1, "entry_price": 1,
         "status": 1}).to_list(200)
    acc = await db.accounts.find_one({"user_id": user_id}) or {}
    equity = float(acc.get("equity") or acc.get("balance") or 0)

    by_sym: dict = {}
    from pip_utils import base_symbol
    for t in open_trades:
        sym = base_symbol((t.get("symbol") or "").upper())
        if not sym:
            continue
        e = by_sym.setdefault(sym, {"notional": 0.0, "positions": 0,
                                    "long": 0, "short": 0})
        e["notional"] += _notional_of(t)
        e["positions"] += 1
        if str(t.get("action") or "").upper().startswith("BUY"):
            e["long"] += 1
        else:
            e["short"] += 1

    if not by_sym:
        return {"symbols": [], "warnings": [], "equity": equity,
                "cvar_target_pct": DEFAULT_CVAR_TARGET_PCT,
                "note": "No open positions — nothing to optimize."}

    syms = sorted(by_sym)
    vols = {}
    for s in syms:
        vols[s] = (await _atr_pct(s)) or 0.01
    corr: dict = {s: {} for s in syms}
    unknown_pairs: list[str] = []
    for i, s1 in enumerate(syms):
        for s2 in syms[i + 1:]:
            rho = await corr_returns(s1, s2)
            if rho is None:
                rho = UNKNOWN_RHO
                unknown_pairs.append(f"{s1}/{s2}")
            corr[s1][s2] = rho
            corr[s2][s1] = rho

    targets = target_weights(vols, corr)
    total_notional = sum(e["notional"] for e in by_sym.values()) or 1.0

    # parametric portfolio CVaR (same math as correlation_kelly)
    port_var = 0.0
    for s1 in syms:
        for s2 in syms:
            rho = 1.0 if s1 == s2 else corr[s1].get(s2, 0.0)
            w1 = by_sym[s1]["notional"] / total_notional
            w2 = by_sym[s2]["notional"] / total_notional
            port_var += w1 * w2 * vols[s1] * vols[s2] * rho
    sigma = math.sqrt(max(port_var, 0.0))
    cvar_pct = (ES_MULT_95 * sigma * total_notional / equity * 100.0
                if equity > 0 else None)

    rows, warnings = [], []
    for s in syms:
        cur = round(by_sym[s]["notional"] / total_notional * 100, 1)
        tgt = round(targets[s] * 100, 1)
        drift = round(cur - tgt, 1)
        rows.append({
            "symbol": s, "positions": by_sym[s]["positions"],
            "long": by_sym[s]["long"], "short": by_sym[s]["short"],
            "notional": round(by_sym[s]["notional"], 2),
            "vol_atr_pct": round(vols[s], 3),
            "current_weight_pct": cur, "target_weight_pct": tgt,
            "drift_pp": drift,
            "suggestion": ("REDUCE" if drift > 15 else
                           "ROOM TO ADD" if drift < -15 else "BALANCED"),
        })
        if cur > CONCENTRATION_MAX_PCT and len(syms) > 1:
            warnings.append(f"{s} is {cur}% of open exposure "
                            f"(> {CONCENTRATION_MAX_PCT}% concentration limit)")
    for s1 in syms:
        for s2, rho in corr[s1].items():
            if s1 < s2 and rho >= CORR_WARN:
                warnings.append(f"{s1}/{s2} correlation {rho:.2f} — "
                                f"positions move together, diversification is illusory")
    for pair in unknown_pairs:
        warnings.append(f"{pair} correlation unknown (insufficient overlapping "
                        f"return history) — assumed conservative ρ={UNKNOWN_RHO}")
    if cvar_pct is not None and cvar_pct > DEFAULT_CVAR_TARGET_PCT:
        warnings.append(f"Portfolio CVaR₉₅ {cvar_pct:.2f}% exceeds the "
                        f"{DEFAULT_CVAR_TARGET_PCT}% daily budget")

    return {
        "symbols": rows,
        "correlations": [{"pair": f"{s1}/{s2}", "rho": round(rho, 3)}
                         for s1 in syms for s2, rho in corr[s1].items() if s1 < s2],
        "portfolio_sigma_pct": round(sigma, 4),
        "cvar_95_pct": round(cvar_pct, 3) if cvar_pct is not None else None,
        "cvar_target_pct": DEFAULT_CVAR_TARGET_PCT,
        "total_notional": round(total_notional, 2),
        "equity": equity,
        "warnings": warnings,
        "note": ("Targets are inverse-volatility weights penalized by average "
                 "positive pairwise correlation. Live per-trade enforcement "
                 "runs in correlation_kelly (trim-only)."),
    }
