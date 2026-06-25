"""Macro-Regime Hard Gate for XAUUSD.

Deterministic rule that blocks XAUUSD trades when FRED macro signals
move adversely for gold. Cache-only reads (no live FRED hits) — safe to
call on every trade evaluation.

Gold's two dominant macro drivers:
  • Real yields  — rising yields raise the opportunity cost of holding non-
                    yielding gold → bearish for XAUUSD longs.
  • USD strength — gold priced in USD; a stronger broad-USD index suppresses
                    gold's quoted price → bearish for XAUUSD longs.

Hard rules (env-overridable thresholds):
  DGS10  WoW  ≥ +25bps  → block BUY  ("real_yield_surge")
  DGS10  WoW  ≤ −25bps  → block SELL ("real_yield_collapse")
  DTWEXBGS WoW  ≥ +1.5% → block BUY  ("usd_surge")
  DTWEXBGS WoW  ≤ −1.5% → block SELL ("usd_collapse")

Returns:
  {
    "ok": bool,                # True → trade may proceed
    "blocked_by": str | None,  # short code if blocked
    "reason": str | None,      # human-readable explanation
    "regime": str,             # current macro regime label
    "audit": [ {name, ok, value, ...}, ... ],
    "evaluated_at": iso,
    "thresholds": {...},
  }

Only applies to XAUUSD — every other symbol returns ok=True with a
"symbol_not_gated" audit row (still surfaces in safety_audit so users
can see the gate was consulted).
"""
from datetime import datetime, timezone
import logging
import os

from database import get_db

logger = logging.getLogger("macro-gate")

GATED_SYMBOLS = {"XAUUSD"}


def _f(env_key: str, default: float) -> float:
    try:
        return float(os.environ.get(env_key, default))
    except Exception:
        return default


def _bool(env_key: str, default: bool) -> bool:
    val = os.environ.get(env_key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


ENABLED            = _bool("MACRO_GATE_ENABLED", True)
YIELD_BUY_BLOCK    = _f("MACRO_YIELD_BUY_BLOCK_BPS",  0.25)  # 25bps WoW rise
YIELD_SELL_BLOCK   = _f("MACRO_YIELD_SELL_BLOCK_BPS", 0.25)  # 25bps WoW drop
DXY_BUY_BLOCK_PCT  = _f("MACRO_DXY_BUY_BLOCK_PCT",    1.5)   # 1.5% WoW rise
DXY_SELL_BLOCK_PCT = _f("MACRO_DXY_SELL_BLOCK_PCT",   1.5)   # 1.5% WoW drop


def thresholds() -> dict:
    return {
        "enabled": ENABLED,
        "yield_buy_block_bps": YIELD_BUY_BLOCK,
        "yield_sell_block_bps": YIELD_SELL_BLOCK,
        "dxy_buy_block_pct": DXY_BUY_BLOCK_PCT,
        "dxy_sell_block_pct": DXY_SELL_BLOCK_PCT,
        "gated_symbols": sorted(GATED_SYMBOLS),
    }


async def _cached_series(db, series_id: str) -> dict | None:
    """Read from fred_cache only — never triggers a live FRED hit."""
    try:
        return await db.fred_cache.find_one({"_id": series_id})
    except Exception as e:  # noqa: BLE001
        logger.error("fred_cache read %s failed: %s", series_id, e)
        return None


def _ok(name: str, value=None, info=None) -> dict:
    row = {"name": name, "ok": True, "value": value}
    if info:
        row["info"] = info
    return row


def _block(name: str, reason: str, value=None) -> dict:
    return {"name": name, "ok": False, "reason": reason, "value": value}


async def evaluate(symbol: str, action: str, db=None) -> dict:
    """Evaluate the macro gate for a proposed trade.

    Symbol-agnostic interface — only enforces on XAUUSD; returns ok=True
    immediately for everything else (with a stamped audit row).

    `db` is optional — defaults to the global Motor client. Tests / callers
    that already hold a db handle can pass it in to avoid the global lookup.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    sym = (symbol or "").upper()
    act = (action or "").upper()

    if not ENABLED:
        return {"ok": True, "blocked_by": None, "reason": None,
                "regime": "gate_disabled",
                "audit": [_ok("macro_gate_enabled", value=False,
                              info="MACRO_GATE_ENABLED=false")],
                "evaluated_at": now_iso, "thresholds": thresholds()}

    if sym not in GATED_SYMBOLS:
        return {"ok": True, "blocked_by": None, "reason": None,
                "regime": "symbol_not_gated",
                "audit": [_ok("symbol_not_gated", value=sym)],
                "evaluated_at": now_iso, "thresholds": thresholds()}

    if act not in ("BUY", "SELL"):
        return {"ok": True, "blocked_by": None, "reason": None,
                "regime": "non_directional_action",
                "audit": [_ok("non_directional_action", value=act)],
                "evaluated_at": now_iso, "thresholds": thresholds()}

    yields = await _cached_series(db or get_db(), "DGS10")
    dxy    = await _cached_series(db or get_db(), "DTWEXBGS")

    audit: list[dict] = []

    # If cache is empty (e.g., no FRED key configured) we fail-open with a
    # clear audit row — better to trade than to block all gold trades when
    # macro data isn't even available.
    if not yields and not dxy:
        audit.append(_ok("macro_data_present", value=False,
                         info="No fred_cache entries — fail-open (allow)"))
        return {"ok": True, "blocked_by": None, "reason": None,
                "regime": "no_macro_data",
                "audit": audit, "evaluated_at": now_iso,
                "thresholds": thresholds()}

    # 10Y yield WoW check (signed)
    if yields:
        try:
            wow = float(yields.get("wow_delta") or 0.0)
        except Exception:
            wow = 0.0
        if act == "BUY" and wow >= YIELD_BUY_BLOCK:
            audit.append(_block(
                "yields_wow_for_buy",
                f"10Y yield +{wow:.2f}pp WoW ≥ +{YIELD_BUY_BLOCK:.2f} → "
                f"rising real yields are bearish for gold longs",
                wow,
            ))
            return {"ok": False, "blocked_by": "real_yield_surge",
                    "reason": (f"10Y Treasury yield surged "
                               f"+{wow:.2f}pp WoW — gold longs blocked"),
                    "regime": "real_yield_surge",
                    "audit": audit, "evaluated_at": now_iso,
                    "thresholds": thresholds()}
        if act == "SELL" and wow <= -YIELD_SELL_BLOCK:
            audit.append(_block(
                "yields_wow_for_sell",
                f"10Y yield {wow:.2f}pp WoW ≤ -{YIELD_SELL_BLOCK:.2f} → "
                f"falling real yields are bullish for gold, shorts blocked",
                wow,
            ))
            return {"ok": False, "blocked_by": "real_yield_collapse",
                    "reason": (f"10Y Treasury yield dropped "
                               f"{wow:.2f}pp WoW — gold shorts blocked"),
                    "regime": "real_yield_collapse",
                    "audit": audit, "evaluated_at": now_iso,
                    "thresholds": thresholds()}
        audit.append(_ok("yields_wow", value=f"{wow:+.2f}pp"))

    # USD broad-index WoW check (% change, signed) — DTWEXBGS is an index
    # level, not a rate. Convert raw delta → % change vs latest.
    if dxy:
        try:
            latest_dxy = float(dxy.get("latest") or 0)
            wow_dxy_raw = float(dxy.get("wow_delta") or 0)
            wow_dxy_pct = (wow_dxy_raw / latest_dxy * 100.0) if latest_dxy else 0.0
        except Exception:
            wow_dxy_pct = 0.0
        if act == "BUY" and wow_dxy_pct >= DXY_BUY_BLOCK_PCT:
            audit.append(_block(
                "dxy_wow_for_buy",
                f"USD index +{wow_dxy_pct:.2f}% WoW ≥ +{DXY_BUY_BLOCK_PCT:.2f}% → "
                f"strong USD is bearish for gold longs",
                wow_dxy_pct,
            ))
            return {"ok": False, "blocked_by": "usd_surge",
                    "reason": (f"USD broad index rallied "
                               f"+{wow_dxy_pct:.2f}% WoW — gold longs blocked"),
                    "regime": "usd_surge",
                    "audit": audit, "evaluated_at": now_iso,
                    "thresholds": thresholds()}
        if act == "SELL" and wow_dxy_pct <= -DXY_SELL_BLOCK_PCT:
            audit.append(_block(
                "dxy_wow_for_sell",
                f"USD index {wow_dxy_pct:.2f}% WoW ≤ -{DXY_SELL_BLOCK_PCT:.2f}% → "
                f"weak USD is bullish for gold, shorts blocked",
                wow_dxy_pct,
            ))
            return {"ok": False, "blocked_by": "usd_collapse",
                    "reason": (f"USD broad index fell "
                               f"{wow_dxy_pct:.2f}% WoW — gold shorts blocked"),
                    "regime": "usd_collapse",
                    "audit": audit, "evaluated_at": now_iso,
                    "thresholds": thresholds()}
        audit.append(_ok("dxy_wow", value=f"{wow_dxy_pct:+.2f}%"))

    return {"ok": True, "blocked_by": None, "reason": None,
            "regime": "macro_neutral",
            "audit": audit, "evaluated_at": now_iso,
            "thresholds": thresholds()}


async def gate_status(db=None) -> dict:
    """User-facing summary used by the Dashboard widget.

    Evaluates both BUY and SELL for XAUUSD using the same cached data, so
    the UI can show 'BUY ALLOWED / SELL BLOCKED' (or vice versa) without
    needing two API hits.
    """
    buy = await evaluate("XAUUSD", "BUY", db=db)
    sell = await evaluate("XAUUSD", "SELL", db=db)
    blocked = []
    if not buy["ok"]:
        blocked.append({"action": "BUY", "blocked_by": buy["blocked_by"],
                        "reason": buy["reason"]})
    if not sell["ok"]:
        blocked.append({"action": "SELL", "blocked_by": sell["blocked_by"],
                        "reason": sell["reason"]})
    return {
        "symbol": "XAUUSD",
        "buy_ok": buy["ok"],
        "sell_ok": sell["ok"],
        "regime": buy["regime"] if not buy["ok"] else
                  (sell["regime"] if not sell["ok"] else "macro_neutral"),
        "blocked": blocked,
        "thresholds": thresholds(),
        "evaluated_at": buy["evaluated_at"],
    }
