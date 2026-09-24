"""iter-171 (#10) — signed, single-use, atomic execution authorizations.

Every live order must carry a server-minted authorization that is:
  • signed   — HMAC-SHA256 over the canonical (user, account, symbol, side,
               nonce) binding, so it can't be forged or retargeted;
  • single-use — a unique nonce + an atomic issued→consumed flip means the
               same authorization can never fire two orders (replay-proof);
  • atomic   — consumption is a single find_one_and_update, safe under
               concurrent dispatch.
"""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

DEFAULT_TTL_SEC = 120


def _secret() -> bytes:
    s = os.environ.get("ORDER_AUTH_SECRET")
    if not s:
        raise RuntimeError("ORDER_AUTH_SECRET must be set to authorize orders")
    return s.encode()


def _canonical(fields: dict) -> str:
    return "|".join(f"{k}={fields[k]}" for k in sorted(fields))


def _sign(fields: dict) -> str:
    return hmac.new(_secret(), _canonical(fields).encode(),
                    hashlib.sha256).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _binding(user_id, account_id, symbol, side, nonce) -> dict:
    return {"user_id": str(user_id), "account_id": str(account_id),
            "symbol": str(symbol), "side": str(side), "nonce": nonce}


async def mint(db, *, user_id, account_id, symbol, side,
               ttl_sec: int = DEFAULT_TTL_SEC) -> dict:
    """Create a signed, single-use authorization bound to this exact order."""
    nonce = secrets.token_urlsafe(24)
    now = _now()
    fields = _binding(user_id, account_id, symbol, side, nonce)
    sig = _sign(fields)
    await db.order_authorizations.insert_one({
        **fields, "signature": sig, "status": "issued",
        "issued_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_sec)).isoformat(),
        "consumed_at": None})
    return {"nonce": nonce, "signature": sig, **fields}


async def consume(db, *, nonce, user_id, account_id, symbol, side) -> dict:
    """Atomically consume the authorization. Returns {ok, reason}. A second
    call with the same nonce returns ok=False (replay/duplicate blocked)."""
    fields = _binding(user_id, account_id, symbol, side, nonce)
    expected = _sign(fields)
    doc = await db.order_authorizations.find_one_and_update(
        {"nonce": nonce, "status": "issued",
         "user_id": fields["user_id"], "account_id": fields["account_id"],
         "symbol": fields["symbol"], "side": fields["side"]},
        {"$set": {"status": "consumed", "consumed_at": _now().isoformat()}})
    if not doc:
        return {"ok": False, "reason": "not_found_or_replayed"}
    if not hmac.compare_digest(str(doc.get("signature", "")), expected):
        return {"ok": False, "reason": "bad_signature"}
    try:
        exp = datetime.fromisoformat(str(doc["expires_at"]))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if _now() > exp:
            return {"ok": False, "reason": "expired"}
    except (KeyError, ValueError):
        pass
    return {"ok": True, "nonce": nonce, "signature": expected}


async def authorize_order(db, *, user_id, account_id, symbol, side) -> dict:
    """Mint + immediately consume a fresh authorization for one order. The
    returned record is stored on the trade as tamper-evident proof."""
    a = await mint(db, user_id=user_id, account_id=account_id,
                   symbol=symbol, side=side)
    used = await consume(db, nonce=a["nonce"], user_id=user_id,
                         account_id=account_id, symbol=symbol, side=side)
    if not used["ok"]:
        return {"ok": False, "reason": used["reason"]}
    return {"ok": True, "nonce": a["nonce"], "signature": a["signature"],
            "authorized_at": _now().isoformat()}
