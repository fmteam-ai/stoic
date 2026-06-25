"""Smart Order Router (SOR) — picks order type / venue / sizing strategy
given the liquidity environment.

Currently STOIC has ONE venue (MT5) but the router still adds value by
choosing:
  - **MARKET vs LIMIT** based on spread quality
  - **Slice the lot via TWAP/VWAP** when the lot is large or liquidity is fair
  - **DEFER** when liquidity is POOR/AVOID and the trade isn't urgent

Future venues (when added): Binance via CCXT for crypto. The router will
extend its `venue` decision automatically — the API contract stays stable.

Output:
  {
    "venue": "MT5",
    "order_type": "MARKET"|"LIMIT"|"DEFER",
    "limit_offset_pips": float|None,  # only for LIMIT
    "slice_strategy": "NONE"|"TWAP"|"VWAP",
    "schedule_minutes": int,
    "reason": str,
  }
"""
import logging

logger = logging.getLogger("execution.smart-router")


def route(*, signal: dict, liquidity: dict) -> dict:
    sym = (signal or {}).get("symbol", "").upper()
    lot = float((signal or {}).get("lot_size") or 0)
    tier = (liquidity or {}).get("tier", "FAIR")
    score = (liquidity or {}).get("score", 50)
    ratio = (liquidity or {}).get("details", {}).get("spread_ratio")

    # 1. Venue (only MT5 today)
    venue = "MT5"
    if sym in {"BTCUSD", "ETHUSD", "SOLUSD"} and tier in {"POOR", "AVOID"}:
        # Future: route crypto to Binance CCXT for tighter spread.
        # For now, surface the intent so the UI can show "Would route to Binance".
        venue = "MT5"

    # 2. DEFER threshold — POOR/AVOID + low score = wait it out
    if tier == "AVOID":
        return {
            "venue": venue,
            "order_type": "DEFER",
            "limit_offset_pips": None,
            "slice_strategy": "NONE",
            "schedule_minutes": 0,
            "reason": f"Liquidity {tier} ({score}/100) — defer until book recovers.",
        }

    # 3. Order type
    if tier in {"EXCELLENT", "GOOD"}:
        order_type = "MARKET"
        limit_offset = None
        reason = f"Liquidity {tier} ({score}/100) — fire MARKET, spread is tight."
    else:
        # FAIR / POOR but not AVOID — use LIMIT a tick or two off the mid-price
        # to avoid eating worse fill. Offset scales with how wide the spread is.
        if ratio and ratio > 1.5:
            limit_offset = round(0.3 * (ratio - 1.0), 2)  # in pips
        else:
            limit_offset = 0.5
        order_type = "LIMIT"
        reason = (f"Liquidity {tier} ({score}/100) — LIMIT ±{limit_offset}pips "
                  f"to avoid widening-spread slippage.")

    # 4. Slicing — TWAP/VWAP for large lots OR when liquidity is fair
    slice_strategy = "NONE"
    schedule_minutes = 0
    if lot >= 0.10 or tier == "FAIR":
        # London/NY peak hours → VWAP (concentrate fills in high-vol windows).
        # Elsewhere → TWAP (equal intervals).
        sess = (liquidity or {}).get("details", {}).get("session")
        if sess in ("london", "ny"):
            slice_strategy = "VWAP"
            schedule_minutes = 10 if lot >= 0.10 else 5
        else:
            slice_strategy = "TWAP"
            schedule_minutes = 15 if lot >= 0.10 else 8
        reason += f" Slice via {slice_strategy} over {schedule_minutes}m."

    return {
        "venue": venue,
        "order_type": order_type,
        "limit_offset_pips": limit_offset,
        "slice_strategy": slice_strategy,
        "schedule_minutes": schedule_minutes,
        "reason": reason,
    }
