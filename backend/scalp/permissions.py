"""Scalp subsystem · control-plane permissions (SLOW path).

Refreshed asynchronously every REFRESH_SEC; the tick fast path reads only
the in-memory cache. Stale cache (> STALE_SEC) fails CLOSED.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger("scalp.permissions")

REFRESH_SEC = 30
STALE_SEC = 120
_cache: dict = {}          # key -> {"ts": float, "perms": dict}
_inflight: set = set()


def _key(user_id: str, symbol: str) -> str:
    return f"{user_id}:{symbol}"


async def _compute(db, user_id: str, symbol: str, cfg) -> dict:
    perms = {"long_enabled": False, "short_enabled": False, "reasons": [],
             "regime": "UNKNOWN", "computed_at": datetime.now(timezone.utc).isoformat()}

    # session window (UTC)
    hour = datetime.now(timezone.utc).hour
    if not (cfg.session_start_utc <= hour < cfg.session_end_utc):
        perms["reasons"].append("outside allowed sessions")
        return perms

    # scheduled-news guard — fail closed on error (news status unknown)
    try:
        from economic_calendar import upcoming_for
        events = await upcoming_for(symbol) or []
        now = datetime.now(timezone.utc)
        for ev in events:
            if str(ev.get("impact", "")).lower() != "high":
                continue
            ts = ev.get("time") or ev.get("timestamp")
            try:
                evt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                # item 20 — a HIGH-impact event we cannot time is UNKNOWN
                # risk for a scalper: fail closed instead of skipping it.
                perms["reasons"].append("news event timestamp invalid — fail closed")
                return perms
            mins = (evt - now).total_seconds() / 60.0
            if -cfg.news_blackout_minutes <= mins <= cfg.news_blackout_minutes:
                perms["reasons"].append(f"news blackout: {ev.get('title') or ev.get('name')}")
                return perms
    except Exception as e:  # noqa: BLE001
        perms["reasons"].append(f"news status unknown ({type(e).__name__}) — fail closed")
        return perms

    # Phase C — deterministic regime classifier with H1 MTF confirmation
    try:
        from scalp import regime as regime_mod
        doc = await db.intraday_candles.find_one(
            {"user_id": user_id, "symbol": symbol, "timeframe": "M15"},
            {"bars": {"$slice": -24}})
        bars = (doc or {}).get("bars") or []
        if len(bars) < 12:
            perms["reasons"].append("insufficient M15 context")
            return perms
        rd = regime_mod.classify(bars, cfg.pip_size)
        perms["regime_detail"] = rd
        perms["ema_slope_pips"] = rd.get("ema_slope_pips")
        if rd["regime"] == "VOLATILITY_SHOCK":
            perms["regime"] = "VOLATILITY_SHOCK"
            perms["reasons"].append("volatility shock — extreme conditions")
            return perms
        if rd["regime"] == "TREND_UP":
            perms["regime"] = "TRENDING_UP"
            perms["long_enabled"] = True
        elif rd["regime"] == "TREND_DOWN":
            perms["regime"] = "TRENDING_DOWN"
            perms["short_enabled"] = True
        else:
            perms["regime"] = "FLAT"
            perms["reasons"].append("no directional regime")
    except Exception as e:  # noqa: BLE001
        perms["reasons"].append(f"regime unavailable ({type(e).__name__}) — fail closed")
    return perms


def get_cached(user_id: str, symbol: str) -> dict:
    """Fast-path read. Missing/stale cache → both directions disabled."""
    ent = _cache.get(_key(user_id, symbol))
    if ent is None or time.time() - ent["ts"] > STALE_SEC:
        return {"long_enabled": False, "short_enabled": False,
                "regime": "UNKNOWN",
                "reasons": ["control-plane permissions stale — fail closed"]}
    return ent["perms"]


def maybe_refresh(db, user_id: str, symbol: str, cfg) -> None:
    """Kick an async refresh if due. Never blocks the tick path."""
    k = _key(user_id, symbol)
    ent = _cache.get(k)
    if ent is not None and time.time() - ent["ts"] < REFRESH_SEC:
        return
    if k in _inflight:
        return
    _inflight.add(k)

    async def _job():
        try:
            perms = await _compute(db, user_id, symbol, cfg)
            _cache[k] = {"ts": time.time(), "perms": perms}
        except Exception as e:  # noqa: BLE001
            logger.warning("scalp permission refresh failed %s: %s", k, e)
        finally:
            _inflight.discard(k)

    try:
        asyncio.get_running_loop().create_task(_job())
    except RuntimeError:
        _inflight.discard(k)
