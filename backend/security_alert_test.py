"""Security-alert channel self-test — proves the security Telegram chat (pairing alerts, security agent)
works BEFORE the demo starts. Reads the same credentials the alerts use; never returns the token."""
from __future__ import annotations

import os
from datetime import datetime, timezone

STATE_ID = "security_telegram_test"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mask_chat(chat: str) -> str:
    chat = str(chat or "")
    return (chat[:2] + "…" + chat[-3:]) if len(chat) > 5 else ("…" if chat else "")


def token_source(vault: bool = False) -> str:
    if vault and (os.environ.get("SECURITY_AGENT_TELEGRAM_BOT_TOKEN") or "").strip():
        return "vault"
    if os.environ.get("SECURITY_AGENT_TELEGRAM_BOT_TOKEN_FILE") and (os.environ.get("SECURITY_AGENT_TELEGRAM_BOT_TOKEN") or "").strip():
        return "secrets_file"
    if (os.environ.get("SECURITY_AGENT_TELEGRAM_BOT_TOKEN") or "").strip():
        return "env"
    return "unset"


async def vault_has_token(db) -> bool:
    return await db.secrets_vault.find_one({"_id": "SECURITY_AGENT_TELEGRAM_BOT_TOKEN"}, {"_id": 1}) is not None


HINT = ("Not configured — click SET on SECURITY_AGENT_TELEGRAM_BOT_TOKEN and SECURITY_AGENT_TELEGRAM_CHAT_ID above "
        "(sealed in the vault, applied to the API now and to the workers within a minute). Alternative: token in "
        "secrets/security_telegram_token + chat id in backend/.env, then docker compose up -d.")


def test_text(actor_email: str, environment: str) -> str:
    return (
        "STOIC · SECURITY ALERT CHANNEL TEST\n"
        f"Sent by {actor_email} from the {environment} API at {_now_iso()[:19]} UTC.\n"
        "If you read this, pairing alerts (VPS silent 10 min) and security-agent findings will reach this chat."
    )


async def status(db) -> dict:
    from security_agent.alerts import telegram_creds
    creds = telegram_creds()
    last = await db.platform_state.find_one({"_id": STATE_ID}) or {}
    return {"configured": bool(creds), "token_source": token_source(await vault_has_token(db)),
            "chat_id_masked": _mask_chat(creds[1]) if creds else "",
            "last_test": {k: last.get(k) for k in ("at", "ok", "by", "detail")} if last else None,
            "hint": "" if creds else HINT}


async def send_test(db, actor: dict) -> dict:
    from security_agent.alerts import send_telegram, telegram_creds
    from audit_chain import append_chained
    creds = telegram_creds()
    if not creds:
        await db.platform_state.update_one({"_id": STATE_ID}, {"$set": {"at": _now_iso(), "ok": False, "by": actor.get("email"), "detail": "not configured"}}, upsert=True)
        return {"ok": False, "detail": "Security Telegram not configured — set the token and chat id on this page (SET), or token in secrets/security_telegram_token + chat id in backend/.env"}
    env = os.environ.get("APP_ENV") or "preview"
    ok = await send_telegram(test_text(actor.get("email") or actor.get("id"), env), creds=creds)
    detail = f"test message delivered to chat {_mask_chat(creds[1])}" if ok else "Telegram rejected the message — check the bot token, the chat id and that the bot is a member of the chat"
    await db.platform_state.update_one({"_id": STATE_ID}, {"$set": {"at": _now_iso(), "ok": ok, "by": actor.get("email"), "detail": detail}}, upsert=True)
    await append_chained(db, {"actor_email": actor.get("email"), "action": "security_alert_test", "target_kind": "telegram",
                              "target_id": _mask_chat(creds[1]), "reason": "operator channel check", "ok": ok, "at": _now_iso()})
    return {"ok": ok, "detail": detail, "chat_id_masked": _mask_chat(creds[1])}
