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
             "regime": "UNKNOWN", "regime_reason": None,
             "computed_at": datetime.now(timezone.utc).isoformat()}

    # Phase C — regime is classified FIRST and ALWAYS so the UI shows the
    # actual market state even when a later gate blocks trading (the old
    # ordering left regime=UNKNOWN whenever session/news returned early).
    regime_ok = False
    try:
        from scalp import regime as regime_mod
        doc = await db.intraday_candles.find_one(
            {"user_id": user_id, "symbol": symbol, "timeframe": "M15"},
            {"bars": {"$slice": -24}})
        bars = (doc or {}).get("bars") or []
        if len(bars) < 12:
            perms["regime_reason"] = (
                f"insufficient M15 history ({len(bars)}/12 bars) — "
                "EA streams candles every 5 min; wait for warm-up")
        else:
            rd = regime_mod.classify(bars, cfg.pip_size)
            perms["regime_detail"] = rd
            perms["ema_slope_pips"] = rd.get("ema_slope_pips")
            regime_ok = True
            if rd["regime"] == "VOLATILITY_SHOCK":
                perms["regime"] = "VOLATILITY_SHOCK"
            elif rd["regime"] == "TREND_UP":
                perms["regime"] = "TRENDING_UP"
            elif rd["regime"] == "TREND_DOWN":
                perms["regime"] = "TRENDING_DOWN"
            else:
                perms["regime"] = "FLAT"
    except Exception as e:  # noqa: BLE001
        perms["regime_reason"] = f"classifier error ({type(e).__name__})"

    # session window (UTC)
    hour = datetime.now(timezone.utc).hour
    if not (cfg.session_start_utc <= hour < cfg.session_end_utc):
        perms["reasons"].append(
            f"outside allowed sessions ({cfg.session_start_utc:02d}-"
            f"{cfg.session_end_utc:02d} UTC)")
        return perms

    # scheduled-news guard — fail closed on error (news status unknown).
    # Events carry epoch `when_ts` + ISO `when` (economic_calendar contract);
    # the old code read non-existent `time`/`timestamp` keys, so EVERY
    # high-impact event within 24h tripped "timestamp invalid — fail closed".
    news_diag = {"provider": None, "status": "UNKNOWN", "events_24h": 0,
                 "next_high_impact": None,
                 "blackout_minutes": cfg.news_blackout_minutes}
    try:
        from economic_calendar import upcoming_for, feed_status
        events = await upcoming_for(symbol) or []
        news_diag.update(feed_status())
        news_diag["events_24h"] = len(events)
        # Provider fully down with an empty cache = we are blind to scheduled
        # news → fail CLOSED (a silent [] here used to fail open).
        if news_diag.get("status") == "DOWN":
            perms["reasons"].append("news feed unavailable — fail closed")
            perms["news"] = news_diag
            return perms
        now = datetime.now(timezone.utc)
        for ev in events:
            if str(ev.get("impact", "")).lower() != "high":
                continue
            ts = ev.get("when_ts")
            if ts is not None:
                evt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            else:
                try:
                    evt = datetime.fromisoformat(
                        str(ev.get("when")).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    # item 20 — a HIGH-impact event we cannot time is UNKNOWN
                    # risk for a scalper: fail closed instead of skipping it.
                    perms["reasons"].append(
                        "news event timestamp invalid — fail closed")
                    perms["news"] = news_diag
                    return perms
            mins = (evt - now).total_seconds() / 60.0
            if news_diag["next_high_impact"] is None and mins >= 0:
                news_diag["next_high_impact"] = {
                    "title": ev.get("title"), "country": ev.get("country"),
                    "when": evt.isoformat(), "minutes_away": round(mins, 1)}
            if -cfg.news_blackout_minutes <= mins <= cfg.news_blackout_minutes:
                perms["reasons"].append(
                    f"news blackout: {ev.get('title') or ev.get('name')}")
                perms["news"] = news_diag
                return perms
        perms["news"] = news_diag
    except Exception as e:  # noqa: BLE001
        news_diag["status"] = "DOWN"
        perms["news"] = news_diag
        perms["reasons"].append(
            f"news status unknown ({type(e).__name__}) — fail closed")
        return perms

    # direction enablement from the (already computed) regime
    if not regime_ok:
        perms["reasons"].append(
            perms["regime_reason"] or "regime unavailable — fail closed")
        return perms
    if perms["regime"] == "VOLATILITY_SHOCK":
        perms["reasons"].append("volatility shock — extreme conditions")
    elif perms["regime"] == "TRENDING_UP":
        perms["long_enabled"] = True
    elif perms["regime"] == "TRENDING_DOWN":
        perms["short_enabled"] = True
    else:
        perms["reasons"].append("no directional regime")
    return perms


def get_cached(user_id: str, symbol: str) -> dict:
    """Fast-path read. Missing/stale cache → both directions disabled."""
    ent = _cache.get(_key(user_id, symbol))
    if ent is None or time.time() - ent["ts"] > STALE_SEC:
        return {"long_enabled": False, "short_enabled": False,
                "regime": "UNKNOWN",
                "regime_reason": ("permissions not refreshed recently — the "
                                  "refresh runs on incoming ticks, so this "
                                  "usually means the tick stream is offline"),
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
