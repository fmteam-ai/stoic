"""Telegram push notifications — fire-and-forget alerts to the app owner.

Each user can configure their own bot_token + chat_id via /api/notifications/telegram.
Credentials are stored AES-256-GCM encrypted in the `notifications` collection.

Events that fire alerts (configurable per user):
  - trade_opened   — when EA fills a new trade
  - trade_closed   — when SL/TP hit or manual close
  - breakeven      — when SL moved to entry
  - partial_close  — when 50% closed at TP1
  - trail          — when trailing SL adjusts
  - circuit_breaker — when daily drawdown trips the killswitch
  - high_conf_signal — when a non-HOLD signal with conf >= 75% generates
  - sl_imminent    — when an open trade's SL ETA drops below 5 minutes
"""
import os
import logging
from datetime import datetime, timezone
from typing import Optional
import httpx
from bson import ObjectId
from bson.errors import InvalidId

from database import get_db
from secrets_vault import decrypt as vault_decrypt

logger = logging.getLogger("notifier")

TELEGRAM_API = "https://api.telegram.org"


async def _get_user_telegram(user_id: str) -> Optional[dict]:
    """Load and decrypt telegram config for a user, or None if disabled/unset."""
    db = get_db()
    doc = await db.notifications.find_one({"user_id": user_id})
    if not doc or not doc.get("telegram_enabled"):
        return None
    token_blob = doc.get("telegram_bot_token")
    chat_id = doc.get("telegram_chat_id")
    if not token_blob or not chat_id:
        return None
    try:
        token = vault_decrypt(token_blob)
    except Exception as e:
        logger.warning("telegram: decrypt failed for user=%s: %s", user_id, e)
        return None
    return {
        "token": token,
        "chat_id": str(chat_id),
        "alerts": doc.get("alerts") or {},
    }


def _esc(text: str) -> str:
    """Escape MarkdownV2 special chars."""
    if text is None:
        return ""
    text = str(text)
    for c in ["\\", "_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
        text = text.replace(c, f"\\{c}")
    return text


# iter-72 · Resolve a human-readable account label for any trade dict /
# trade_id / explicit account_id. Cached per-call by the caller — every
# notify_* helper calls this once before composing the title.
async def _account_label_for(
    trade_or_id=None, *, account_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """Return the account's label (e.g. "vtmarkets", "micro") or "" if unknown.

    Accepts either:
      • a trade dict (must carry `account_id`),
      • a string trade_id (will look up the trade), OR
      • an explicit account_id kwarg.

    Always returns a string; on any error returns "" so callers can
    `if label: ...` cleanly.
    """
    db = get_db()
    try:
        # Resolve account_id from whatever was passed.
        if account_id is None:
            if isinstance(trade_or_id, dict):
                account_id = trade_or_id.get("account_id")
            elif trade_or_id:
                try:
                    oid = trade_or_id if isinstance(trade_or_id, ObjectId) \
                          else ObjectId(str(trade_or_id))
                    tdoc = await db.trades.find_one(
                        {"_id": oid}, projection={"account_id": 1}
                    )
                    account_id = (tdoc or {}).get("account_id")
                except (InvalidId, TypeError, ValueError):
                    return ""
        if not account_id:
            return ""
        try:
            acc_oid = (account_id if isinstance(account_id, ObjectId)
                       else ObjectId(str(account_id)))
        except (InvalidId, TypeError, ValueError):
            return ""
        adoc = await db.accounts.find_one(
            {"_id": acc_oid}, projection={"label": 1, "broker": 1}
        )
        if not adoc:
            return ""
        return adoc.get("label") or adoc.get("broker") or ""
    except Exception as e:
        logger.debug("account_label_for failed: %s", e)
        return ""


def _title_with_account(account_label: str, title: str) -> str:
    """Prefix a title with the account label if present."""
    if not account_label:
        return title
    # Format: "🟢 [vtmarkets] Trade Opened · XAUUSD BUY"
    # If title starts with an emoji + space, insert label after the emoji.
    if title and title[0] not in ("[", " ") and " " in title:
        head, rest = title.split(" ", 1)
        # Treat the first token as emoji if it's short and not ascii-letters.
        if len(head) <= 3 and not head.isascii():
            return f"{head} [{account_label}] {rest}"
    return f"[{account_label}] {title}"


async def send_telegram(user_id: str, event_type: str, title: str, lines: list) -> bool:
    """Fire a Telegram alert to a user. Returns True on 200 OK.

    Always wraps in try/except — never raises into the call site. Trade execution
    must never be blocked by a notification failure.
    """
    try:
        cfg = await _get_user_telegram(user_id)
        if not cfg:
            return False
        # Respect per-event opt-out (default to enabled if missing)
        alerts = cfg.get("alerts") or {}
        if alerts.get(event_type, True) is False:
            return False

        body_lines = [f"*{_esc(title)}*", ""]
        body_lines.extend(_esc(line) for line in lines)
        body_lines.append("")
        body_lines.append(_esc("— STOIC AI Trader"))
        text = "\n".join(body_lines)

        url = f"{TELEGRAM_API}/bot{cfg['token']}/sendMessage"
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(url, json={
                "chat_id": cfg["chat_id"],
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            })
        ok = (r.status_code == 200)
        if not ok:
            logger.warning("telegram send failed user=%s status=%s body=%s", user_id, r.status_code, r.text[:200])
        return ok
    except Exception as e:
        logger.warning("telegram send exception user=%s: %s", user_id, e)
        return False


# ------------- Event helpers (composes message bodies) -------------

async def _is_plausible_trade(trade: dict) -> bool:
    """Reject obviously-synthetic trades that should never trigger a real notification.

    Tests historically use round numbers (entry=2400.5, sl=2385, tp=2430 for XAU
    at a 2024-era price) and dummy mt5_tickets (999111, 999222). These leak into
    Telegram if a test runner hits /api/bridge/report directly. This is the
    last-line defence below the pytest conftest.
    """
    # 0. PRIMARY GUARD: the trade must actually exist in the DB. A real call
    # site always passes a dict freshly loaded from / inserted into the
    # `trades` collection, so it carries an `_id` (or its serialized `id`
    # form when the dict has been prepared for a WS broadcast). Direct
    # synthetic invocations hand in a plain dict with neither — they fail
    # this check and never reach the user.
    trade_id = trade.get("_id") or trade.get("id")
    if not trade_id:
        return False
    try:
        oid = trade_id if isinstance(trade_id, ObjectId) else ObjectId(str(trade_id))
    except (InvalidId, TypeError, ValueError):
        return False
    db = get_db()
    persisted = await db.trades.find_one({"_id": oid}, projection={"_id": 1})
    if not persisted:
        return False

    # 1. Mock tickets — real MT5 tickets are 8-10 digit unique IDs, never < 1,000,000
    ticket = trade.get("mt5_ticket")
    if ticket is not None:
        try:
            t_int = int(str(ticket))
            if 0 < t_int < 1_000_000:
                return False
        except (TypeError, ValueError):
            pass

    # 2. Entry price sanity — must be within 50% of live market price.
    entry = trade.get("entry_price")
    sym = trade.get("symbol")
    if entry and sym:
        try:
            from market import get_quote
            q = await get_quote(sym)
            live = (q or {}).get("price")
            if live and live > 0:
                ratio = entry / live
                if ratio < 0.5 or ratio > 2.0:
                    return False
        except Exception:
            pass  # if quote fails, be permissive — don't block real alerts
    return True


async def notify_trade_opened(user_id: str, trade: dict) -> bool:
    if not await _is_plausible_trade(trade):
        logger.warning(
            "Refusing to send 'trade_opened' notification for implausible trade: "
            "symbol=%s entry=%s mt5_ticket=%s (probable test fixture)",
            trade.get("symbol"), trade.get("entry_price"), trade.get("mt5_ticket"),
        )
        return False
    origin = trade.get("origin", "AI signal")
    is_external = (origin == "external") or bool(trade.get("external_open"))
    account_label = await _account_label_for(trade)

    # External trades — opened on MT5 outside STOIC (manual click, another EA,
    # or a test). Use a visibly different title + event key so the user can
    # opt out separately and never confuses them with bot-initiated trades.
    if is_external:
        arrow = "📌"
        title = _title_with_account(account_label,
            f"{arrow} Manual Trade Detected · {trade.get('symbol')} {trade.get('action')}")
        body_lines = [
            f"Lots: {trade.get('lot_size')}",
            f"Entry: {trade.get('entry_price')}",
            "",
            "⚠ This was NOT opened by STOIC — it came from your MT5 terminal",
            "(manual click, another EA, or the broker). STOIC is tracking it",
            "for P&L reporting only.",
        ]
        return await send_telegram(user_id, "external_trade_opened", title, body_lines)

    # Bot-initiated trade — the normal STOIC alert.
    arrow = "🟢" if trade.get("action") == "BUY" else "🔵"
    return await send_telegram(user_id, "trade_opened",
        _title_with_account(account_label,
            f"{arrow} Trade Opened · {trade.get('symbol')} {trade.get('action')}"),
        [
            f"Lots: {trade.get('lot_size')}",
            f"Entry: {trade.get('entry_price')}",
            f"SL: {trade.get('stop_loss')}  TP: {trade.get('take_profit')}",
            f"Origin: {origin}",
        ])


async def notify_trade_closed(user_id: str, trade: dict) -> None:
    if not await _is_plausible_trade(trade):
        logger.warning(
            "Refusing to send 'trade_closed' notification for implausible trade: "
            "symbol=%s entry=%s mt5_ticket=%s (probable test fixture)",
            trade.get("symbol"), trade.get("entry_price"), trade.get("mt5_ticket"),
        )
        return
    pnl = float(trade.get("pnl") or 0)
    emoji = "✅" if pnl >= 0 else "❌"
    sign = "+" if pnl >= 0 else ""
    account_label = await _account_label_for(trade)
    await send_telegram(user_id, "trade_closed",
        _title_with_account(account_label,
            f"{emoji} Trade Closed · {trade.get('symbol')} {trade.get('action')}"),
        [
            f"P&L: {sign}{pnl:.2f}",
            f"Entry: {trade.get('entry_price')}  Exit: {trade.get('exit_price')}",
            f"Lots: {trade.get('lot_size')}",
        ])


async def notify_breakeven(user_id: str, trade_id: str, new_sl, r_multiple) -> None:
    account_label = await _account_label_for(trade_id)
    await send_telegram(user_id, "breakeven",
        _title_with_account(account_label, "🛡 Break-Even Set"),
        [
            f"Trade: {trade_id[-6:]}",
            f"SL moved to entry @ {new_sl}",
            f"Reached +{r_multiple}R — trade now zero-risk.",
        ])


async def notify_partial_close(user_id: str, trade_id: str, from_lot, to_lot, r_multiple) -> None:
    account_label = await _account_label_for(trade_id)
    await send_telegram(user_id, "partial_close",
        _title_with_account(account_label, "✂️ Partial Close at TP1"),
        [
            f"Trade: {trade_id[-6:]}",
            f"Closed: {round(from_lot - to_lot, 2)} lots @ +{r_multiple}R",
            f"Remaining: {to_lot} lots running for TP2",
        ])


async def notify_trail(user_id: str, trade_id: str, new_sl, r_multiple) -> None:
    account_label = await _account_label_for(trade_id)
    await send_telegram(user_id, "trail",
        _title_with_account(account_label, "📈 SL Trailed"),
        [
            f"Trade: {trade_id[-6:]}",
            f"New SL: {new_sl}",
            f"Locking in +{r_multiple}R move",
        ])


async def notify_circuit_breaker(user_id: str, reason: str, today_pnl, equity,
                                 account_id: Optional[str] = None) -> None:
    account_label = await _account_label_for(account_id=account_id, user_id=user_id)
    await send_telegram(user_id, "circuit_breaker",
        _title_with_account(account_label, "🚨 CIRCUIT BREAKER TRIPPED"),
        [
            "Bot has been auto-stopped.",
            f"Reason: {reason}",
            f"Today's P&L: {today_pnl}",
            f"Equity: {equity}",
            "",
            "Trades remain open with their existing SL/TP.",
            "Restart from Bot Config when you're ready.",
        ])


async def notify_high_conf_signal(user_id: str, signal: dict) -> None:
    # Signals are pre-trade — they may not yet know which account will execute.
    # Tag with account label only if the signal carries an account_id hint.
    account_label = await _account_label_for(signal) if signal.get("account_id") else ""
    await send_telegram(user_id, "high_conf_signal",
        _title_with_account(account_label,
            f"🎯 High-Confidence Signal · {signal.get('symbol')} {signal.get('action')}"),
        [
            f"Confidence: {signal.get('confidence')}%",
            f"Entry: {signal.get('entry_price')}",
            f"SL: {signal.get('stop_loss')}  TP: {signal.get('take_profit')}",
        ])


async def notify_pre_news_close(user_id: str, trade_id: str, symbol: str, event_title: str, minutes_until: float) -> None:
    account_label = await _account_label_for(trade_id)
    await send_telegram(user_id, "pre_news_protect",
        _title_with_account(account_label, f"🛡 Pre-News Protect · {symbol}"),
        [
            f"Trade: {trade_id[-6:]}",
            f"Flattening before '{event_title}' in {minutes_until:.0f}min.",
            "Position will be re-evaluated after the event settles.",
        ])


async def notify_loss_review(user_id: str, review: dict) -> None:
    """iter-54 · Auto Loss Review — aggregate loss analysis with
    shadow-tested counter-measures."""
    agg = review.get("aggregates") or {}
    lines = [
        f"Analysed {agg.get('losses')} losses (${abs(agg.get('loss_pnl') or 0):.2f}) "
        f"over {agg.get('window_days')}d.",
        "",
    ]
    diag = (review.get("diagnosis") or "").strip()
    if diag:
        lines.append(diag)
        lines.append("")
    for m in (review.get("measures") or [])[:3]:
        ev = m.get("evidence")
        tag = ""
        if ev and ev.get("testable"):
            tag = (f" → net ${ev['net_effect']:+.2f} over {ev['shadow_days']}d "
                   f"(saves ${ev['losses_avoided']:.0f} / misses ${ev['wins_missed']:.0f})")
        lines.append(f"• [{(m.get('priority') or 'med').upper()}] {m.get('title')}{tag}")
    lines.append("")
    lines.append("Review & apply in Loss Lab — measures are never auto-applied.")
    await send_telegram(user_id, "loss_review",
        "🔬 Auto Loss Review — suggested measures", lines)


async def notify_friday_flat(user_id: str, closed: int, tightened: int, actions: list) -> None:
    """iter-52 · Friday Flat guard summary — positions closed/tightened ahead
    of the Friday 21:00 UTC weekly close (weekend gap protection)."""
    lines = []
    for tr, act, new_sl in actions[:8]:
        sym = tr.get("symbol")
        side = tr.get("action")
        if act == "close":
            lines.append(f"• {sym} {side} — closing (weekend flat)")
        else:
            lines.append(f"• {sym} {side} — SL tightened to {new_sl}")
    if len(actions) > 8:
        lines.append(f"…and {len(actions) - 8} more")
    lines.append("")
    lines.append("Weekend gap protection ahead of the Friday 21:00 UTC close.")
    await send_telegram(user_id, "friday_flat",
        f"🛡 Friday Flat · {closed} closed · {tightened} tightened", lines)


async def notify_account_blocked(user_id: str, account_id: str, retcode: str,
                                 retcode_label: str, hint: str) -> None:
    """iter-72 · Push alert when the broker-rejection breaker auto-halts an
    account (e.g. VT Markets retcode 10013 ×3). Lets the user act in seconds
    rather than discovering through Bot Health later."""
    account_label = await _account_label_for(account_id=account_id, user_id=user_id)
    await send_telegram(user_id, "account_blocked",
        _title_with_account(account_label, "🚫 Account Auto-Halted"),
        [
            f"Broker error: {retcode_label} ({retcode})",
            f"Cause: {hint}",
            "",
            "Bot will skip this account until you click 'Resume Trading'",
            "(or set a symbol_suffix) on the Accounts page.",
        ])
