"""SL-imminent watcher — Telegram alerts when an open trade's SL is close.

For every open trade with a stop-loss:
  1. Pull the latest live quote for the symbol (no API call — uses cached
     `get_quote`, falls back to the most-recent row in `price_ticks`).
  2. Estimate price velocity from the last 6 distinct rows in `price_ticks`
     for that symbol (median |Δp|/Δt across consecutive samples).
  3. Compute price-distance from current to SL, divide by velocity → ETA secs.
  4. If ETA < THRESHOLD_SECONDS (default 300s = 5 minutes) AND we haven't
     already alerted for this (trade_id, sl_level) → fire Telegram alert and
     stamp `sl_alert_sent_at` on the trade so we don't spam.

If the SL is moved (e.g. break-even shift), the stamp is reset because the
key includes the SL price — moving SL means new alert window.
"""
import logging
import statistics
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from database import get_db
from market import get_quote
from notifier import send_telegram
from pip_utils import price_to_pips

logger = logging.getLogger("sl_watcher")

THRESHOLD_SECONDS = 300       # 5 minutes — fire alert below this
HISTORY_WINDOW_MIN = 30       # look back this many minutes for velocity
MIN_SAMPLES_FOR_VELOCITY = 2  # need at least this many distinct ticks
COOLDOWN_MIN = 10             # re-fire only after this many minutes (same SL)


async def _recent_velocity(db, symbol: str) -> float:
    """Return median |Δprice|/Δsec across the last 30 minutes of ticks.

    Returns 0.0 if not enough samples (caller treats as "no ETA").
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=HISTORY_WINDOW_MIN)
    cursor = (
        db.price_ticks
        .find({"symbol": symbol.upper(), "ts": {"$gte": since}})
        .sort("ts", 1)
        .limit(50)
    )
    ticks = await cursor.to_list(length=50)
    if len(ticks) < MIN_SAMPLES_FOR_VELOCITY:
        return 0.0
    rates = []
    for i in range(1, len(ticks)):
        dt = (ticks[i]["ts"] - ticks[i - 1]["ts"]).total_seconds()
        if dt <= 0:
            continue
        rates.append(abs(float(ticks[i]["price"]) - float(ticks[i - 1]["price"])) / dt)
    if not rates:
        return 0.0
    return float(statistics.median(rates))


def _sl_key(trade: dict) -> str:
    """Idempotency key — composed of trade id + current SL price.

    Moving SL (BE shift / trail) changes the key so the next imminent
    condition gets a fresh alert.
    """
    return f"{trade.get('_id')}::{trade.get('stop_loss')}"


async def sweep_once() -> dict:
    """Single sweep — to be called from the bot_runner loop."""
    db = get_db()
    cursor = db.trades.find({"status": "open", "stop_loss": {"$gt": 0}})
    trades = await cursor.to_list(length=500)

    fired = 0
    checked = 0
    now = datetime.now(timezone.utc)

    # Cache quote per symbol within a single sweep
    quote_cache: dict = {}
    velocity_cache: dict = {}

    for t in trades:
        symbol = t.get("symbol")
        sl = t.get("stop_loss")
        if not symbol or not sl:
            continue
        try:
            sl = float(sl)
        except (TypeError, ValueError):
            continue

        if symbol not in quote_cache:
            try:
                quote_cache[symbol] = await get_quote(symbol)
            except Exception:
                quote_cache[symbol] = None
        q = quote_cache.get(symbol)
        if not q or q.get("price") is None:
            continue
        price = float(q["price"])

        if symbol not in velocity_cache:
            velocity_cache[symbol] = await _recent_velocity(db, symbol)
        velocity = velocity_cache[symbol]
        if velocity <= 0:
            continue  # no measurable movement → can't ETA

        # Action-aware: an alert only matters if price is moving TOWARDS the SL.
        # BUY trade SL is below entry → alert when price is falling toward SL.
        # SELL trade SL is above entry → alert when price is rising toward SL.
        action = t.get("action")
        if action == "BUY" and price <= sl:
            continue  # already past SL — broker should be closing it now
        if action == "SELL" and price >= sl:
            continue

        price_dist = abs(price - sl)
        eta_secs = price_dist / velocity
        checked += 1
        if eta_secs > THRESHOLD_SECONDS:
            continue

        # Idempotency / cooldown
        key = _sl_key(t)
        last_key = t.get("sl_alert_key")
        last_at = t.get("sl_alert_sent_at")
        if last_key == key and last_at:
            try:
                last_dt = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
                if (now - last_dt).total_seconds() < COOLDOWN_MIN * 60:
                    continue
            except ValueError:
                pass

        # Fire Telegram alert
        pips_away = price_to_pips(symbol, price_dist)
        eta_label = "imminent" if eta_secs < 60 else f"~{int(round(eta_secs / 60))}m"
        title = f"⚠ SL Imminent · {symbol} {action}"
        body = [
            f"Current: {price:.5g}  SL: {sl:.5g}",
            f"Distance: ~{pips_away:.1f} pips",
            f"ETA at current velocity: {eta_label}",
            f"Trade: {t.get('lot_size')} lot",
        ]
        sent = await send_telegram(
            t.get("user_id"),
            event_type="sl_imminent",
            title=title,
            lines=body,
        )
        if sent:
            fired += 1
        # Always mark to avoid spamming even if Telegram failed transiently
        await db.trades.update_one(
            {"_id": ObjectId(str(t["_id"]))},
            {"$set": {
                "sl_alert_key": key,
                "sl_alert_sent_at": now.isoformat(),
                "sl_alert_eta_secs": round(eta_secs, 1),
            }},
        )

    return {"checked": checked, "fired": fired}
