"""Scalp Radar — Telegram ping when live M15 alignment crosses the scalp
threshold (60), i.e. the moment the intraday scalp engine arms for a trade.

State is kept per user+symbol in `scalp_radar_state` with 60/50 hysteresis so
borderline oscillation doesn't spam. A direction flip while armed re-pings.
"""
import logging
from datetime import datetime, timezone

from intraday_features import intraday_alignment

logger = logging.getLogger("scalp-radar")

ARM_AT = 60
DISARM_AT = 50


async def scalp_radar_ping(db, user_id: str, symbol: str, pack: dict | None) -> None:
    if not pack:
        return
    scores = {a: intraday_alignment(a, pack) for a in ("BUY", "SELL")}
    direction = max(scores, key=lambda a: scores[a][0])
    score, note = scores[direction]

    key = {"user_id": user_id, "symbol": symbol}
    st = await db.scalp_radar_state.find_one(key) or {}
    armed = bool(st.get("armed"))

    if score >= ARM_AT and (not armed or st.get("direction") != direction):
        await db.scalp_radar_state.update_one(key, {"$set": {
            "armed": True, "direction": direction, "score": score,
            "armed_at": datetime.now(timezone.utc).isoformat(),
        }}, upsert=True)
        from notifier import send_telegram
        await send_telegram(
            user_id, "scalp_radar",
            f"🎯 Scalp radar ARMED · {symbol} {direction}",
            [
                f"M15 alignment: {score}/100",
                f"Structure: {note}",
                f"Price: {pack.get('last_price')} · ATR15 {pack.get('atr15')}",
                f"Day range: {pack.get('day_range_pct')}%",
                "The intraday engine can now fire in this direction — "
                "watch for entry once all guardrails clear.",
            ],
        )
        logger.info("scalp radar armed user=%s sym=%s dir=%s score=%s", user_id, symbol, direction, score)
    elif score < DISARM_AT and armed:
        await db.scalp_radar_state.update_one(key, {"$set": {
            "armed": False, "score": score,
            "disarmed_at": datetime.now(timezone.utc).isoformat(),
        }})
