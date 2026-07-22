"""Telegram 2-way control plane — handles incoming commands via webhook.

Security model:
  - Each user has a unique `webhook_secret` (32 chars URL-safe) generated on activation.
  - Telegram is configured (setWebhook) to POST to /api/telegram/webhook/{secret}.
  - The webhook handler:
      1. Looks up the user by webhook_secret.
      2. Verifies the incoming message.chat.id matches the user's configured chat_id.
         (Prevents impersonation if the secret URL ever leaks.)
      3. Dispatches the command and replies via sendMessage.

  - Activation/deactivation requires JWT auth via /api/notifications/telegram/* — never via webhook.
"""
import os
import secrets
import logging
from datetime import datetime, timezone
from typing import Optional
from bson import ObjectId
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
import httpx

from auth import get_current_user
from database import get_db
from secrets_vault import decrypt as vault_decrypt

logger = logging.getLogger("telegram-control")

router = APIRouter(prefix="/telegram", tags=["telegram"])
TELEGRAM_API = "https://api.telegram.org"


# ---------- Helpers ----------

def _esc(text) -> str:
    """Escape MarkdownV2 special chars."""
    if text is None:
        return ""
    text = str(text)
    for c in ["\\", "_", "*", "[", "]", "(", ")", "~", "`", ">", "#", "+", "-", "=", "|", "{", "}", ".", "!"]:
        text = text.replace(c, f"\\{c}")
    return text


async def _send_reply(token: str, chat_id, text: str) -> None:
    """Send a Markdown-V2 reply. Errors are logged but never raised."""
    url = f"{TELEGRAM_API}/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            })
        if r.status_code != 200:
            logger.warning("telegram reply non-200: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        logger.warning("telegram reply exception: %s", e)


async def _load_user_by_secret(secret: str) -> Optional[dict]:
    db = get_db()
    return await db.notifications.find_one({"webhook_secret": secret})


# ---------- Command handlers ----------

async def _cmd_help(token, chat_id) -> None:
    text = (
        "*🤖 STOIC AI Trader · Commands*\n\n"
        "/status — Bot state, last signal, next tick\n"
        "/pnl — Today's & cumulative P&L\n"
        "/trades — List open trades\n"
        "/balance — Account balances\n"
        "/run — Start the bot\n"
        "/stop — Stop the bot\n"
        "/panic — Emergency stop \\+ close all\n"
        "/close — Close all open trades\n"
        "/close XAUUSD — Close XAUUSD trades only\n"
        "/help — This message"
    )
    await _send_reply(token, chat_id, text)


async def _cmd_status(token, chat_id, user_id) -> None:
    db = get_db()
    cfg = await db.bot_configs.find_one({"user_id": user_id})
    last = await db.signals.find_one({"user_id": user_id}, sort=[("created_at", -1)])
    open_trades = await db.trades.count_documents({
        "user_id": user_id, "status": {"$in": ["pending", "open"]}
    })
    active = bool(cfg and cfg.get("active"))
    parts = [
        "*🤖 Bot Status*",
        f"State: {'🟢 ACTIVE' if active else '🔴 STOPPED'}",
        f"Risk: {_esc(cfg.get('risk_level','medium') if cfg else 'medium')}",
        f"Symbols: {_esc(' · '.join(cfg.get('symbols', []) if cfg else []))}",
        f"Open trades: {open_trades}",
        f"Auto\\-execute: {'ON' if (cfg and cfg.get('auto_execute')) else 'OFF'}",
    ]
    if last:
        parts.append("")
        parts.append(f"*Last signal · {_esc(last.get('symbol'))}*")
        parts.append(f"Action: {_esc(last.get('action'))} · {_esc(last.get('confidence'))}%")
    await _send_reply(token, chat_id, "\n".join(parts))


async def _cmd_pnl(token, chat_id, user_id) -> None:
    db = get_db()
    today_utc = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_cursor = db.trades.find({
        "user_id": user_id, "status": "closed", "closed_at": {"$gte": today_utc}
    })
    today_trades = await today_cursor.to_list(length=500)
    today_pnl = sum(float(t.get("pnl") or 0) for t in today_trades)

    all_cursor = db.trades.find({"user_id": user_id, "status": "closed"})
    all_trades = await all_cursor.to_list(length=2000)
    total_pnl = sum(float(t.get("pnl") or 0) for t in all_trades)
    wins = sum(1 for t in all_trades if (t.get("pnl") or 0) > 0)
    losses = len(all_trades) - wins
    wr = round(100 * wins / max(1, len(all_trades)), 1)

    today_sign = "\\+" if today_pnl >= 0 else ""
    total_sign = "\\+" if total_pnl >= 0 else ""
    text = (
        "*📊 P&L Snapshot*\n\n"
        f"Today: `{today_sign}{_esc(round(today_pnl, 2))}` "
        f"\\({len(today_trades)} trades\\)\n"
        f"Total: `{total_sign}{_esc(round(total_pnl, 2))}` "
        f"\\({len(all_trades)} trades\\)\n"
        f"Win rate: `{_esc(wr)}%` \\({wins}W · {losses}L\\)"
    )
    await _send_reply(token, chat_id, text)


async def _cmd_trades(token, chat_id, user_id) -> None:
    db = get_db()
    cursor = db.trades.find({
        "user_id": user_id, "status": {"$in": ["pending", "open"]}
    }).sort("opened_at", -1)
    trades = await cursor.to_list(length=15)
    if not trades:
        await _send_reply(token, chat_id, "*📂 Open Trades*\n\nNo open trades right now\\.")
        return
    lines = ["*📂 Open Trades*", ""]
    for t in trades:
        emoji = "🟢" if t.get("action") == "BUY" else "🔵"
        flags = []
        if t.get("partial_closed"):
            flags.append("PC")
        if t.get("breakeven_set"):
            flags.append("BE")
        if t.get("trail_active"):
            flags.append("TRAIL")
        flag_str = " " + " ".join(f"`{f}`" for f in flags) if flags else ""
        lines.append(
            f"{emoji} {_esc(t.get('symbol'))} {_esc(t.get('action'))} "
            f"`{_esc(t.get('lot_size'))}` @ `{_esc(t.get('entry_price'))}`{flag_str}"
        )
    await _send_reply(token, chat_id, "\n".join(lines))


async def _cmd_balance(token, chat_id, user_id) -> None:
    db = get_db()
    cursor = db.accounts.find({"user_id": user_id})
    accts = await cursor.to_list(length=20)
    if not accts:
        await _send_reply(token, chat_id, "*💼 Accounts*\n\nNo accounts configured\\.")
        return
    lines = ["*💼 Account Balances*", ""]
    for a in accts:
        status = "🟢" if a.get("status") == "connected" else "⚪"
        mode = (a.get("mode") or "live").upper()
        bal = round(float(a.get("balance") or 0), 2)
        eq = round(float(a.get("equity") or 0), 2)
        lines.append(
            f"{status} {_esc(a.get('label'))} \\[{_esc(mode)}\\]\n"
            f"  Balance: `{_esc(bal)}` · Equity: `{_esc(eq)}`"
        )
    await _send_reply(token, chat_id, "\n".join(lines))


async def _cmd_run(token, chat_id, user_id) -> None:
    db = get_db()
    # Broadcast: turn ON every bot the user owns. Telegram has no concept
    # of "which account" — the safe default is to control all of them.
    res = await db.bot_configs.update_many(
        {"user_id": user_id},
        {"$set": {"active": True, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    n = res.modified_count
    msg = (
        f"*✅ Bot Started*\n\nAll {n} bot{'s' if n != 1 else ''} active and scanning markets\\."
        if n > 0 else
        "*⚠️ No bots to start*\n\nYou don't have any bot configs yet \\— add one in the dashboard\\."
    )
    await _send_reply(token, chat_id, msg)


async def _cmd_stop(token, chat_id, user_id) -> None:
    db = get_db()
    res = await db.bot_configs.update_many(
        {"user_id": user_id},
        {"$set": {"active": False, "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    n = res.modified_count
    msg = (
        f"*🛑 Bot Stopped*\n\nAll {n} bot{'s' if n != 1 else ''} paused\\. No new signals\\. Open trades remain on their SL/TP\\."
        if n > 0 else
        "*⚠️ Nothing to stop*\n\nNo active bot configs found\\."
    )
    await _send_reply(token, chat_id, msg)


async def _cmd_panic(token, chat_id, user_id) -> None:
    """Same logic as /api/panic but invoked from telegram (no http call needed)."""
    db = get_db()
    # Stop ALL bots (broadcast — PANIC's whole purpose is "stop everything")
    bot_res = await db.bot_configs.update_many(
        {"user_id": user_id}, {"$set": {"active": False}}
    )
    # Cancel pending trades
    cancel_res = await db.trades.update_many(
        {"user_id": user_id, "status": "pending"},
        {"$set": {"status": "cancelled", "closed_at": datetime.now(timezone.utc).isoformat(),
                  "close_reason": "panic"}},
    )
    # Mark open for close
    close_res = await db.trades.update_many(
        {"user_id": user_id, "status": "open"},
        {"$set": {"close_requested": True, "close_reason": "panic"}},
    )
    text = (
        "*🚨 PANIC ENGAGED*\n\n"
        f"All {bot_res.modified_count} bot{'s' if bot_res.modified_count != 1 else ''} stopped\\.\n"
        f"Cancelled pending: `{cancel_res.modified_count}`\n"
        f"Marked open for close: `{close_res.modified_count}`"
    )
    await _send_reply(token, chat_id, text)


async def _cmd_close(token, chat_id, user_id, args: str) -> None:
    db = get_db()
    symbol = args.strip().upper() if args else None
    query = {"user_id": user_id, "status": "open"}
    if symbol:
        query["symbol"] = symbol
    res = await db.trades.update_many(query, {"$set": {"close_requested": True, "close_reason": "manual_telegram"}})
    if symbol:
        text = f"*✂️ Close Requested*\n\nMarked `{res.modified_count}` open {_esc(symbol)} trade\\(s\\) for close\\."
    else:
        text = f"*✂️ Close All Requested*\n\nMarked `{res.modified_count}` open trade\\(s\\) for close\\."
    await _send_reply(token, chat_id, text)


async def _dispatch_command(token, chat_id, user_id, text: str) -> None:
    """Parse and route a single command."""
    text = (text or "").strip()
    if not text.startswith("/"):
        await _send_reply(token, chat_id, "Send /help to see available commands\\.")
        return
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower().split("@")[0]  # strip @botname suffix
    args = parts[1] if len(parts) > 1 else ""

    handlers = {
        "/start": _cmd_help,
        "/help": _cmd_help,
        "/status": _cmd_status,
        "/pnl": _cmd_pnl,
        "/trades": _cmd_trades,
        "/balance": _cmd_balance,
        "/run": _cmd_run,
        "/start_bot": _cmd_run,
        "/stop": _cmd_stop,
        "/panic": _cmd_panic,
    }
    if cmd == "/close":
        await _cmd_close(token, chat_id, user_id, args)
        return
    handler = handlers.get(cmd)
    if handler is None:
        await _send_reply(token, chat_id, f"Unknown command `{_esc(cmd)}`\\. Send /help to see what I can do\\.")
        return
    # /help and /start don't need user_id
    if handler in (_cmd_help,):
        await handler(token, chat_id)
    else:
        await handler(token, chat_id, user_id)


# ---------- Webhook endpoint ----------

@router.post("/incoming/{secret}")
async def telegram_webhook(secret: str, request: Request):
    """Receive an Update from Telegram. Best-effort: always return 200 so Telegram doesn't retry."""
    try:
        user_doc = await _load_user_by_secret(secret)
        if not user_doc:
            return {"ok": True}  # silently ignore unknown secret
        if not user_doc.get("telegram_bot_token") or not user_doc.get("telegram_chat_id"):
            return {"ok": True}

        body = await request.json()
        message = (body or {}).get("message") or (body or {}).get("edited_message") or {}
        chat = message.get("chat") or {}
        incoming_chat_id = str(chat.get("id") or "")
        expected_chat_id = str(user_doc.get("telegram_chat_id") or "")
        if not incoming_chat_id or incoming_chat_id != expected_chat_id:
            logger.warning("telegram webhook: chat_id mismatch (got=%s expected=%s)",
                           incoming_chat_id, expected_chat_id)
            return {"ok": True}

        text = (message.get("text") or "").strip()
        if not text:
            return {"ok": True}

        try:
            token = vault_decrypt(user_doc["telegram_bot_token"])
        except Exception as e:
            logger.warning("telegram webhook: decrypt failed: %s", e)
            return {"ok": True}

        await _dispatch_command(token, incoming_chat_id, user_doc["user_id"], text)
        return {"ok": True}
    except Exception as e:
        logger.exception("telegram webhook handler crashed: %s", e)
        return {"ok": True}


# ---------- Activation endpoints (JWT-protected) ----------

class WebhookEnableRequest(BaseModel):
    base_url: str  # e.g. https://stoic-trading.preview.emergentagent.com


@router.post("/webhook/enable")
async def enable_webhook(payload: WebhookEnableRequest, user=Depends(get_current_user)):
    """Generate a new webhook_secret, register it with Telegram, persist it locally."""
    db = get_db()
    doc = await db.notifications.find_one({"user_id": user["id"]})
    if not doc or not doc.get("telegram_bot_token"):
        raise HTTPException(status_code=400, detail="Configure your Telegram bot token first")

    try:
        token = vault_decrypt(doc["telegram_bot_token"])
    except Exception:
        raise HTTPException(status_code=500, detail="Could not decrypt bot token")

    new_secret = secrets.token_urlsafe(32)
    base = payload.base_url.rstrip("/")
    webhook_url = f"{base}/api/telegram/incoming/{new_secret}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.post(
                f"{TELEGRAM_API}/bot{token}/setWebhook",
                json={"url": webhook_url, "allowed_updates": ["message"]},
            )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"Network error: {e}")
    if r.status_code != 200 or not r.json().get("ok"):
        try:
            desc = r.json().get("description") or "Telegram rejected the webhook"
        except Exception:
            desc = "Telegram rejected the webhook"
        raise HTTPException(status_code=400, detail=desc)

    await db.notifications.update_one(
        {"user_id": user["id"]},
        {"$set": {
            "webhook_secret": new_secret,
            "webhook_url": webhook_url,
            "webhook_enabled_at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {"ok": True, "webhook_url": webhook_url}


@router.post("/webhook/disable")
async def disable_webhook(user=Depends(get_current_user)):
    db = get_db()
    doc = await db.notifications.find_one({"user_id": user["id"]})
    if not doc or not doc.get("telegram_bot_token"):
        return {"ok": True}
    try:
        token = vault_decrypt(doc["telegram_bot_token"])
    except Exception:
        token = None
    if token:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(f"{TELEGRAM_API}/bot{token}/deleteWebhook")
            except Exception:
                pass  # best-effort
    await db.notifications.update_one(
        {"user_id": user["id"]},
        {"$unset": {"webhook_secret": "", "webhook_url": "", "webhook_enabled_at": ""}},
    )
    return {"ok": True}


@router.get("/webhook/status")
async def webhook_status(user=Depends(get_current_user)):
    db = get_db()
    doc = await db.notifications.find_one({"user_id": user["id"]}) or {}
    return {
        "enabled": bool(doc.get("webhook_secret")),
        "webhook_url": doc.get("webhook_url"),
        "enabled_at": doc.get("webhook_enabled_at"),
    }
