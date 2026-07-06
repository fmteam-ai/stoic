"""Friday Flat guard — iter-52. Weekend gap protection.

Positions held over the weekend can gap straight through their stop at the
Sunday re-open (exactly what produced the four big Jul-3→Jul-5 losses).
This guard runs in the final stretch before the Friday 21:00 UTC weekly
close (matches microstructure.py market hours) and, per the user's config:

  • mode "close"   — queue FULL_CLOSE on every open non-crypto position
                     (requires EA v1.40+; older EAs degrade to "tighten").
  • mode "tighten" — move SL to breakeven when in profit, otherwise halve
                     the remaining SL risk (never loosens a stop).

New entries are blocked for the whole window by the bot_runner gate.
Crypto (BTCUSD/ETHUSD) is exempt — it trades through the weekend.
"""
import logging
from datetime import datetime, timezone, timedelta

from bson import ObjectId

from pip_utils import base_symbol
from ws_manager import manager as ws_manager

logger = logging.getLogger("friday-flat")

CRYPTO_BASES = {"BTCUSD", "ETHUSD"}
WEEKLY_CLOSE_UTC_HOUR = 21  # Friday 21:00 UTC — matches microstructure.py


def in_friday_flat_window(cfg: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    enabled = cfg.get("friday_flat_enabled", True)
    minutes = int(cfg.get("friday_flat_minutes_before") or 60)
    out = {"in_window": False, "enabled": bool(enabled), "minutes_before": minutes}
    if not enabled or now.weekday() != 4:  # Friday only
        return out
    close_dt = now.replace(hour=WEEKLY_CLOSE_UTC_HOUR, minute=0, second=0, microsecond=0)
    start = close_dt - timedelta(minutes=minutes)
    # Keep blocking a few hours past the close — the market is shut anyway
    # and this prevents any race right at 21:00.
    out["in_window"] = start <= now < close_dt + timedelta(hours=3)
    out["close_at"] = close_dt.isoformat()
    return out


def _ea_supports_full_close(acc: dict) -> bool:
    try:
        parts = str(acc.get("ea_version") or "0").split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        return (major, minor) >= (1, 40)
    except (ValueError, TypeError):
        return False


def _tightened_sl(tr: dict) -> float | None:
    """Breakeven when in profit; otherwise halve the remaining SL risk.
    Returns None when there's nothing safe to tighten (never loosens)."""
    entry = float(tr.get("entry_price") or 0)
    if entry <= 0:
        return None
    action = tr.get("action")
    px = float(tr.get("live_price") or 0) or entry
    sl = float(tr.get("stop_loss") or 0)
    in_profit = px > entry if action == "BUY" else px < entry
    if in_profit:
        new_sl = entry
    elif sl > 0:
        new_sl = (sl + px) / 2.0
    else:
        return None
    if action == "BUY" and sl and new_sl <= sl:
        return None
    if action == "SELL" and sl and new_sl >= sl:
        return None
    return round(new_sl, 5)


async def sweep_user(db, user_id: str, cfg: dict) -> dict:
    """Apply Friday Flat for one bot config. Idempotent per trade
    (friday_flat_at flag). Returns {"closed": n, "tightened": n}."""
    stats = {"closed": 0, "tightened": 0}
    ff = in_friday_flat_window(cfg)
    if not ff["in_window"]:
        return stats
    mode = (cfg.get("friday_flat_mode") or "close").lower()
    q = {
        "user_id": user_id,
        "status": "open",
        "close_requested": {"$ne": True},
        "friday_flat_at": {"$exists": False},
    }
    cfg_account_id = cfg.get("account_id")
    if cfg_account_id:
        q["account_id"] = cfg_account_id
    else:
        # Default cfg only manages accounts WITHOUT their own per-account cfg.
        ov = await db.bot_configs.find(
            {"user_id": user_id, "account_id": {"$nin": [None]}},
            {"account_id": 1},
        ).to_list(length=50)
        ov_ids = [o["account_id"] for o in ov if o.get("account_id")]
        if ov_ids:
            q["account_id"] = {"$nin": ov_ids}
    open_trades = await db.trades.find(q).to_list(length=200)
    if not open_trades:
        return stats

    now_iso = datetime.now(timezone.utc).isoformat()
    acc_cache: dict = {}
    actions: list = []
    for tr in open_trades:
        if base_symbol(tr.get("symbol")) in CRYPTO_BASES:
            continue  # crypto trades the weekend — no gap risk
        if tr.get("pending_modification"):
            continue  # don't stomp an in-flight management action
        aid = tr.get("account_id")
        if aid not in acc_cache:
            try:
                acc_cache[aid] = await db.accounts.find_one({"_id": ObjectId(aid)})
            except Exception:
                acc_cache[aid] = None
        acc = acc_cache[aid] or {}
        eff = mode
        if eff == "close" and not _ea_supports_full_close(acc):
            eff = "tighten"  # EA <v1.40 ignores FULL_CLOSE — degrade gracefully
        if eff == "close":
            update = {
                "close_requested": True,
                "close_reason_pending": "friday_flat",
                "friday_flat_at": now_iso,
                "friday_flat_action": "close",
                "pending_modification": {
                    "type": "FULL_CLOSE",
                    "reason": "friday_flat",
                    "requested_at": now_iso,
                },
            }
            stats["closed"] += 1
            actions.append((tr, "close", None))
        else:
            new_sl = _tightened_sl(tr)
            if new_sl is None:
                continue
            update = {
                "friday_flat_at": now_iso,
                "friday_flat_action": "tighten",
                "pending_modification": {
                    "type": "MODIFY_SL",
                    "new_sl": new_sl,
                    "reason": "friday_flat",
                    "requested_at": now_iso,
                },
            }
            stats["tightened"] += 1
            actions.append((tr, "tighten", new_sl))
        await db.trades.update_one({"_id": tr["_id"]}, {"$set": update})
        logger.warning(
            "Friday Flat %s user=%s sym=%s trade=%s%s",
            eff, user_id, tr.get("symbol"), tr["_id"],
            f" new_sl={new_sl}" if eff == "tighten" else "",
        )

    if actions:
        await ws_manager.broadcast(user_id, "friday_flat", {
            "closed": stats["closed"],
            "tightened": stats["tightened"],
            "close_at": ff.get("close_at"),
        })
        try:
            from notifier import notify_friday_flat
            await notify_friday_flat(user_id, stats["closed"], stats["tightened"], actions)
        except Exception as e:
            logger.debug("notify_friday_flat failed: %s", e)
    return stats


async def sweep_all(db) -> dict:
    total = {"closed": 0, "tightened": 0}
    cfgs = await db.bot_configs.find({"active": True}).to_list(length=200)
    for cfg in cfgs:
        try:
            s = await sweep_user(db, cfg["user_id"], cfg)
            total["closed"] += s["closed"]
            total["tightened"] += s["tightened"]
        except Exception as e:
            logger.exception("friday-flat sweep failed user=%s: %s", cfg.get("user_id"), e)
    return total


__all__ = ["in_friday_flat_window", "sweep_user", "sweep_all"]
