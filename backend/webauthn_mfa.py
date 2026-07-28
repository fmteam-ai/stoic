"""iter-177 — WebAuthn (passkeys / hardware security keys) for administrators.

Admins enroll passkeys under Settings → Security and can then satisfy step-up
verification with a passkey instead of a TOTP code. Challenges are single-use,
TTL-bound documents (db.webauthn_challenges); credentials persist the COSE
public key + sign counter for cloned-authenticator detection
(db.webauthn_credentials). RP ID defaults to the request origin's hostname
(leading "www." stripped so apex + www share credentials) and can be pinned
via WEBAUTHN_RP_ID in production.
"""
import base64
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import options_to_json_dict
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

CHALLENGE_TTL_SECONDS = 300
RP_NAME = os.environ.get("WEBAUTHN_RP_NAME", "STOIC AI Trading")
MAX_CREDENTIALS_PER_USER = 10


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def rp_id_for(origin: str) -> str:
    pinned = (os.environ.get("WEBAUTHN_RP_ID") or "").strip()
    if pinned:
        return pinned
    host = urlparse(origin).hostname or ""
    return host[4:] if host.startswith("www.") else host


def check_origin(origin: str, rp_id: str) -> None:
    """The ceremony origin must be http(s) and its host must equal the RP ID
    or be a subdomain of it."""
    p = urlparse(origin or "")
    host = p.hostname or ""
    if p.scheme not in ("https", "http") or not host:
        raise ValueError("invalid origin")
    if not (host == rp_id or host.endswith("." + rp_id)):
        raise ValueError("origin not allowed for this RP ID")


async def _store_challenge(db, user_id: str, purpose: str, action: str | None,
                           challenge: bytes, rp_id: str, origin: str) -> str:
    challenge_id = f"wch_{secrets.token_urlsafe(24)}"
    await db.webauthn_challenges.insert_one({
        "challenge_id": challenge_id, "user_id": user_id,
        "purpose": purpose, "action": action,
        "challenge": _b64u(challenge), "rp_id": rp_id, "origin": origin,
        "expires_at": datetime.now(timezone.utc)
        + timedelta(seconds=CHALLENGE_TTL_SECONDS)})
    return challenge_id


async def _pop_challenge(db, challenge_id: str, user_id: str,
                         purpose: str) -> dict:
    """Single-use: atomically consumed; expired docs also rejected here (the
    TTL monitor only sweeps every ~60s)."""
    doc = await db.webauthn_challenges.find_one_and_delete(
        {"challenge_id": challenge_id, "user_id": user_id,
         "purpose": purpose})
    if not doc:
        raise ValueError("challenge expired or already used")
    exp = doc["expires_at"]
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        raise ValueError("challenge expired or already used")
    return doc


async def list_credentials(db, user_id: str) -> list:
    out = []
    async for c in db.webauthn_credentials.find({"user_id": user_id}):
        out.append({"credential_id": c["credential_id"],
                    "label": c.get("label") or "passkey",
                    "device_type": c.get("device_type"),
                    "backed_up": bool(c.get("backed_up")),
                    "transports": c.get("transports") or [],
                    "created_at": c.get("created_at"),
                    "last_used_at": c.get("last_used_at")})
    return out


async def begin_registration(db, user: dict, origin: str) -> dict:
    rp_id = rp_id_for(origin)
    check_origin(origin, rp_id)
    existing = await db.webauthn_credentials.find(
        {"user_id": user["id"]}).to_list(MAX_CREDENTIALS_PER_USER)
    if len(existing) >= MAX_CREDENTIALS_PER_USER:
        raise ValueError("passkey limit reached — remove one first")
    options = generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME,
        user_id=user["id"].encode(), user_name=user["email"],
        user_display_name=user.get("name") or user["email"],
        exclude_credentials=[
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(c["credential_id"]))
            for c in existing],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED),
        timeout=120000)
    challenge_id = await _store_challenge(
        db, user["id"], "register", None, options.challenge, rp_id, origin)
    return {"challenge_id": challenge_id,
            "options": options_to_json_dict(options)}


async def complete_registration(db, user: dict, challenge_id: str,
                                credential: dict, label: str = "") -> dict:
    ch = await _pop_challenge(db, challenge_id, user["id"], "register")
    v = verify_registration_response(
        credential=credential,
        expected_challenge=base64url_to_bytes(ch["challenge"]),
        expected_rp_id=ch["rp_id"], expected_origin=ch["origin"])
    now = datetime.now(timezone.utc).isoformat()
    doc = {"user_id": user["id"],
           "credential_id": _b64u(v.credential_id),
           "public_key": _b64u(v.credential_public_key),
           "sign_count": v.sign_count,
           "device_type": v.credential_device_type.value,
           "backed_up": v.credential_backed_up,
           "transports": (credential.get("response") or {}).get(
               "transports") or [],
           "rp_id": ch["rp_id"],
           "label": (label or "").strip()[:60] or "passkey",
           "created_at": now, "last_used_at": None}
    await db.webauthn_credentials.insert_one(doc)
    return {"credential_id": doc["credential_id"], "label": doc["label"],
            "device_type": doc["device_type"]}


async def remove_credential(db, user_id: str, credential_id: str) -> bool:
    r = await db.webauthn_credentials.delete_one(
        {"user_id": user_id, "credential_id": credential_id})
    return r.deleted_count == 1


async def begin_step_up(db, user: dict, origin: str, action: str) -> dict:
    rp_id = rp_id_for(origin)
    check_origin(origin, rp_id)
    creds = await db.webauthn_credentials.find(
        {"user_id": user["id"]}).to_list(MAX_CREDENTIALS_PER_USER)
    if not creds:
        raise ValueError("no passkeys enrolled")
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[
            PublicKeyCredentialDescriptor(
                id=base64url_to_bytes(c["credential_id"]))
            for c in creds],
        user_verification=UserVerificationRequirement.PREFERRED,
        timeout=120000)
    challenge_id = await _store_challenge(
        db, user["id"], "step_up", action, options.challenge, rp_id, origin)
    return {"challenge_id": challenge_id,
            "options": options_to_json_dict(options)}


async def complete_step_up(db, user: dict, challenge_id: str, action: str,
                           credential: dict) -> None:
    """Verifies the assertion; raises ValueError on any failure."""
    ch = await _pop_challenge(db, challenge_id, user["id"], "step_up")
    if ch.get("action") != action:
        raise ValueError("challenge was issued for a different action")
    cred = await db.webauthn_credentials.find_one(
        {"user_id": user["id"], "credential_id": credential.get("id")})
    if not cred:
        raise ValueError("unknown credential")
    v = verify_authentication_response(
        credential=credential,
        expected_challenge=base64url_to_bytes(ch["challenge"]),
        expected_rp_id=ch["rp_id"], expected_origin=ch["origin"],
        credential_public_key=base64url_to_bytes(cred["public_key"]),
        credential_current_sign_count=cred["sign_count"])
    await db.webauthn_credentials.update_one(
        {"_id": cred["_id"]},
        {"$set": {"sign_count": v.new_sign_count,
                  "last_used_at": datetime.now(timezone.utc).isoformat()}})


async def has_passkey(db, user_id: str) -> bool:
    return await db.webauthn_credentials.count_documents(
        {"user_id": user_id}, limit=1) > 0
