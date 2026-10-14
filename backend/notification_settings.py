"""Per-user notification settings — ONE dedicated document (`kind: "telegram_settings"`) in db.notifications.

v1.60.32 item 2: the Telegram binding used to be written with an unsorted update_one({"user_id"}) — i.e. onto the
user's OLDEST db.notifications row, which is usually an in-app notice (`security_new_login`, …). Deleting that notice
deleted the Telegram config. Every reader/writer now goes through this module; a legacy row carrying the fields is
migrated once (fields copied to the settings doc, then stripped from the notice).

v1.60.32 item 3: a separate, verifiable alert e-mail (`alert_email`, `alert_email_verified`) — alerts go there once
verified, otherwise to the login e-mail.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

KIND = "telegram_settings"
ALERT_EMAIL_CATEGORIES = ("trading", "outage", "renewal", "support", "digest")
SETTINGS_FIELDS = ("telegram_enabled", "telegram_bot_token", "telegram_chat_id", "telegram_verified",
                   "telegram_verified_at", "telegram_verified_chat_id", "telegram_verify", "alerts", "last_test",
                   "webhook_secret", "webhook_url", "webhook_enabled_at", "updated_at",
                   "alert_email", "alert_email_verified", "alert_email_verified_at", "alert_email_verify",
                   "alert_email_categories")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]+$")


def settings_filter(user_id: str) -> dict:
    return {"user_id": user_id, "kind": KIND}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_settings(db, user_id: str) -> dict:
    """The settings doc ({} when none). Migrates a legacy notice-row binding exactly once."""
    doc = await db.notifications.find_one(settings_filter(user_id))
    if doc:
        return doc
    legacy = await db.notifications.find_one({
        "user_id": user_id, "kind": {"$ne": KIND},
        "$or": [{k: {"$exists": True}} for k in ("telegram_bot_token", "telegram_chat_id", "telegram_enabled",
                                                 "alerts", "webhook_secret", "last_test")]})
    if not legacy:
        return {}
    payload = {k: legacy[k] for k in SETTINGS_FIELDS if k in legacy}
    payload.update({"user_id": user_id, "kind": KIND, "migrated_from": str(legacy["_id"]),
                    "migrated_at": _now().isoformat()})
    try:
        await db.notifications.insert_one(payload)
    except DuplicateKeyError:
        pass
    await db.notifications.update_one({"_id": legacy["_id"]},
                                      {"$unset": {k: "" for k in SETTINGS_FIELDS if k in legacy}})
    return await db.notifications.find_one(settings_filter(user_id)) or payload


async def update_settings(db, user_id: str, update: dict) -> None:
    """`update` is a Mongo update document ($set / $unset / $inc); the identity keys are always pinned."""
    sets = update.setdefault("$set", {})
    sets.update({"user_id": user_id, "kind": KIND})
    await get_settings(db, user_id)          # migrate first so the upsert never creates a second binding
    await db.notifications.update_one(settings_filter(user_id), update, upsert=True)


async def by_webhook_secret(db, secret: str) -> dict | None:
    return await db.notifications.find_one({"kind": KIND, "webhook_secret": secret})


# ── alert e-mail ──────────────────────────────────────────────────────────────────────────────────────────────
def valid_email(addr: str) -> bool:
    return bool(_EMAIL_RE.match((addr or "").strip())) and len(addr.strip()) <= 254


async def alert_email_for(db, user_id: str, login_email: str | None = None, category: str = "trading") -> str | None:
    """Verified alert address when the category is routed there, login e-mail otherwise.

    Categories: trading (test + trade alerts) · outage (VPS/EA silent) · renewal (subscription) · support (ticket
    replies) · digest (weekly digest). Security mail (login OTP, new-login, password reset) never leaves the login
    address and does not go through here."""
    s = await get_settings(db, user_id)
    routed = (s.get("alert_email_categories") or {}).get(category, True)
    if s.get("alert_email") and s.get("alert_email_verified") is True and routed:
        return s["alert_email"]
    if login_email:
        return login_email
    from bson import ObjectId
    u = await db.users.find_one({"_id": ObjectId(user_id)}, {"email": 1}) if ObjectId.is_valid(user_id) else None
    return (u or {}).get("email")


def _code_hash(user_id: str, addr: str, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{addr.lower()}:{code}".encode()).hexdigest()


async def start_email_verification(db, user_id: str) -> tuple[str, str]:
    """Arm a 10-minute challenge for the stored alert_email → (address, code). Caller delivers the code."""
    s = await get_settings(db, user_id)
    addr = (s.get("alert_email") or "").strip()
    if not addr:
        raise ValueError("no_alert_email")
    code = f"{secrets.randbelow(1000000):06d}"
    await update_settings(db, user_id, {"$set": {
        "alert_email_verified": False,
        "alert_email_verify": {"hash": _code_hash(user_id, addr, code), "attempts": 0,
                               "expires_at": (_now() + timedelta(minutes=10)).isoformat()}}})
    return addr, code


async def confirm_email_verification(db, user_id: str, code: str) -> str:
    """Returns an error code ('' on success): no_pending_challenge | challenge_expired | too_many_attempts | wrong_code."""
    s = await get_settings(db, user_id)
    ch = s.get("alert_email_verify") or {}
    addr = (s.get("alert_email") or "").strip()
    code = re.sub(r"\D", "", str(code or ""))
    if not ch or not code or not addr:
        return "no_pending_challenge"
    if datetime.fromisoformat(ch["expires_at"]) < _now():
        return "challenge_expired"
    if int(ch.get("attempts") or 0) >= 5:
        return "too_many_attempts"
    if not hmac.compare_digest(ch["hash"], _code_hash(user_id, addr, code)):
        await update_settings(db, user_id, {"$inc": {"alert_email_verify.attempts": 1}})
        return "wrong_code"
    await update_settings(db, user_id, {"$set": {"alert_email_verified": True, "alert_email_verify": None,
                                                 "alert_email_verified_at": _now().isoformat()}})
    return ""
