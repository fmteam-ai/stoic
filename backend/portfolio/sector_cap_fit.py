"""Pre-trade sector-cap fit — iter-58.

The portfolio risk manager has post-trade auto-deleverage that closes
positions when a sector cap is breached. That's effective but reactive:
the bot opens, the deleverager closes, the user sees an aborted trade.

This helper runs BEFORE `engine.execute` and answers:
  "If I open this proposed lot, will sector X breach its cap?"
If yes, it returns either:
  • a trimmed lot that fits exactly inside the cap, OR
  • `lot=0` when the cap is already breached by existing positions alone
    (no room for new exposure — caller should skip the trade).

Combined with iter-51 correlation-Kelly trim and iter-55 per-account
circuit-breaker isolation, this completes the lot-sizing pipeline:

  raw Kelly  →  max-lot cap  →  correlation/CVaR trim  →  sector-cap fit

`scale ∈ [0, 1]` is always multiplicative — never inflates. Caller logs
the reason for the trim so users can see WHY their bot is sizing down.
"""
from __future__ import annotations
import logging
from typing import Iterable

from portfolio.sectors import sector_for
from portfolio.risk_manager import SECTOR_CAPS_PCT, _resolve_sector_caps

logger = logging.getLogger("portfolio.sector-cap-fit")

# Safety margin below the cap — never sizes the new trade to exactly 100% of
# the cap because tick-to-tick price drift could push us over immediately.
SAFETY_MARGIN_PCT = 0.95


def _notional_of(p: dict) -> float:
    sym = (p.get("symbol") or "").upper()
    return (float(p.get("lot_size") or 0)
            * float(p.get("entry_price") or 0)
            * (100 if sym == "XAUUSD" else 1))


def fit_lot_to_sector_cap(
    *,
    new_symbol: str,
    new_lot: float,
    new_entry_price: float,
    open_positions: Iterable[dict],
    equity: float,
    cfg: dict | None = None,
) -> dict:
    """Trim `new_lot` so the post-trade sector exposure stays inside its cap.

    Returns:
      {
        "lot": float,            # adjusted lot (≤ new_lot, possibly 0)
        "scale": float,          # in (0, 1] — new_lot multiplier
        "reason": str,           # human readable summary
        "sector": str,
        "cap_pct": float,
        "current_pct": float,
        "post_pct": float,
        "would_breach": bool,
      }
    """
    sym = (new_symbol or "").upper()
    sector = sector_for(sym)
    caps = _resolve_sector_caps((cfg or {}).get("sector_caps_pct_override"))
    cap_pct = float(caps.get(sector, 100.0))

    base = {
        "lot": float(new_lot), "scale": 1.0,
        "sector": sector, "cap_pct": cap_pct,
        "current_pct": 0.0, "post_pct": 0.0, "would_breach": False,
        "reason": f"sector {sector} cap {cap_pct:.0f}% — no action needed",
    }

    if equity <= 0 or new_lot <= 0 or new_entry_price <= 0:
        return base

    # Current notional in this sector from existing positions.
    sector_notional_current = 0.0
    for p in open_positions or []:
        if sector_for((p.get("symbol") or "").upper()) != sector:
            continue
        if (p.get("status") or "open") not in ("open", "pending"):
            continue
        sector_notional_current += _notional_of(p)
    current_pct = (sector_notional_current / equity * 100.0) if equity > 0 else 0.0

    # Proposed new-trade notional (XAU has 100× multiplier, others 1×).
    new_mult = 100 if sym == "XAUUSD" else 1
    proposed_new_notional = new_lot * new_entry_price * new_mult
    post_notional = sector_notional_current + proposed_new_notional
    post_pct = (post_notional / equity * 100.0) if equity > 0 else 0.0

    base["current_pct"] = round(current_pct, 2)
    base["post_pct"] = round(post_pct, 2)

    # No breach — proposed trade fits inside the cap.
    if post_pct <= cap_pct:
        base["reason"] = (f"sector {sector}: current {current_pct:.1f}% + new trade → "
                          f"{post_pct:.1f}% / cap {cap_pct:.0f}% (OK)")
        return base

    base["would_breach"] = True

    # Cap already breached by existing positions alone — no room for more.
    if current_pct >= cap_pct * SAFETY_MARGIN_PCT:
        return {**base, "lot": 0.0, "scale": 0.0,
                "reason": (f"sector {sector} already at {current_pct:.1f}% (cap {cap_pct:.0f}%) "
                           "from existing positions — skipping new trade.")}

    # Headroom available — compute the largest lot that fits at SAFETY_MARGIN of cap.
    headroom_notional = (cap_pct * SAFETY_MARGIN_PCT / 100.0 * equity) - sector_notional_current
    if headroom_notional <= 0:
        return {**base, "lot": 0.0, "scale": 0.0,
                "reason": (f"sector {sector} headroom exhausted at {current_pct:.1f}% "
                           f"(cap {cap_pct:.0f}%) — skipping.")}

    max_lot_in_cap = headroom_notional / (new_entry_price * new_mult)
    # Round DOWN to broker minimum step (0.01) so we never round up into a breach.
    trimmed_lot = max(0.0, round(max_lot_in_cap - 0.005, 2))
    if trimmed_lot <= 0:
        return {**base, "lot": 0.0, "scale": 0.0,
                "reason": (f"sector {sector} headroom too tight for any 0.01-lot trade "
                           f"(current {current_pct:.1f}% / cap {cap_pct:.0f}%).")}

    scale = trimmed_lot / new_lot if new_lot > 0 else 0.0
    return {
        **base, "lot": float(trimmed_lot), "scale": float(scale),
        "post_pct": round((sector_notional_current + trimmed_lot * new_entry_price * new_mult)
                          / equity * 100.0, 2),
        "reason": (f"sector {sector} cap fit: {new_lot} → {trimmed_lot} "
                   f"(current {current_pct:.1f}% → post {base['post_pct']:.1f}% / cap {cap_pct:.0f}%)"),
    }
