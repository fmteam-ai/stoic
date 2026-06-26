"""Autonomous deleveraging sweep — runs on the bot_runner cron tick.

For each active user, builds the portfolio snapshot; if `needs_deleveraging`
is true AND we're past the per-account cooldown, executes the action list
and emits a notification (WS + Telegram) so the user knows what fired.

Cooldown defaults to 15 min — prevents thrashing when a trigger oscillates
around its threshold. Env-overridable via `AUTO_DELEVERAGE_COOLDOWN_MIN`.

Returns a summary dict for logging:
  {"checked": int, "triggered": int, "closed": int, "skipped": int}
"""
from datetime import datetime, timezone, timedelta
import logging
import os

from bson import ObjectId

from portfolio.risk_manager import build_snapshot, execute_deleveraging_actions
from ws_manager import manager as ws_manager
try:
    from notifier import send_telegram
except Exception:  # noqa: BLE001
    async def send_telegram(*args, **kwargs):  # fallback no-op
        return False

logger = logging.getLogger("auto-deleverage")


def _enabled() -> bool:
    return os.environ.get("AUTO_DELEVERAGE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


def _cooldown_minutes() -> int:
    try:
        return int(os.environ.get("AUTO_DELEVERAGE_COOLDOWN_MIN", "15"))
    except Exception:
        return 15


async def _on_cooldown(acc: dict) -> bool:
    last = acc.get("auto_deleverage_last_at")
    if not last:
        return False
    try:
        ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except Exception:
        return False
    cooldown = timedelta(minutes=_cooldown_minutes())
    return (datetime.now(timezone.utc) - ts) < cooldown


async def sweep(db) -> dict:
    """Walk every connected MT5 account and auto-deleverage if needed."""
    if not _enabled():
        return {"checked": 0, "triggered": 0, "closed": 0, "skipped": 0,
                "disabled": True}

    summary = {"checked": 0, "triggered": 0, "closed": 0,
               "cancelled": 0, "skipped": 0}
    # Only run on live/paper accounts that have at least one open position
    open_trade_accounts = await db.trades.distinct(
        "account_id", {"status": {"$in": ["open", "pending"]}},
    )
    if not open_trade_accounts:
        return summary

    for acc_id_str in open_trade_accounts:
        try:
            acc = await db.accounts.find_one({"_id": ObjectId(acc_id_str)})
        except Exception:
            continue
        if not acc:
            continue
        summary["checked"] += 1

        if await _on_cooldown(acc):
            continue

        positions = await db.trades.find({
            "user_id": acc["user_id"],
            "account_id": acc_id_str,
            "status": {"$in": ["open", "pending"]},
        }).to_list(length=200)
        if not positions:
            continue

        try:
            snap = await build_snapshot(db, account=acc, open_positions=positions)
        except Exception as e:  # noqa: BLE001
            logger.exception("snapshot build failed for %s: %s", acc_id_str, e)
            continue

        if not snap.get("needs_deleveraging"):
            continue

        summary["triggered"] += 1
        result = await execute_deleveraging_actions(
            db, user_id=acc["user_id"], actions=snap["actions"],
        )
        summary["closed"] += result["closed"]
        summary["cancelled"] += result.get("cancelled", 0)
        summary["skipped"] += result["skipped"]

        # Persist cooldown stamp
        try:
            await db.accounts.update_one(
                {"_id": acc["_id"]},
                {"$set": {
                    "auto_deleverage_last_at": datetime.now(timezone.utc).isoformat(),
                    "auto_deleverage_last_triggers": snap["triggers"],
                    "auto_deleverage_last_closed": result["closed"],
                }},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("cooldown stamp persist failed: %s", e)

        # Notify the user — WS for the dashboard banner, Telegram for off-app reach
        triggers_str = " · ".join(snap["triggers"]).upper()
        await ws_manager.broadcast(acc["user_id"], "auto_deleverage", {
            "account_id": acc_id_str,
            "triggers": snap["triggers"],
            "closed": result["closed"],
            "actions": snap["actions"],
        })
        try:
            await send_telegram(
                acc["user_id"],
                f"🛡 *Auto-Deleverage Fired*\n"
                f"Triggers: {triggers_str}\n"
                f"Closed: {result['closed']} position(s)\n"
                f"DD: {snap['drawdown']['dd_pct']:.2f}% · "
                f"VaR95: {snap['var']['var_95_pct_equity']:.2f}%",
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("Telegram alert failed: %s", e)

        logger.warning(
            "AUTO-DELEVERAGE fired account=%s triggers=%s closed=%d",
            acc_id_str, triggers_str, result["closed"],
        )

    return summary
