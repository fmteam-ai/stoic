"""Installer device attestation (audit r26 P1-02).

`installer_attested` — the only EX5 proof that admits live capital — can be
produced by exactly one path: a signature from the ENROLLED installer device
key over a fresh server nonce. The bridge token (which the EA also holds) can
enrol nothing and attest nothing.

  enrol   : the installer generates a key pair and presents the PUBLIC key while
            redeeming the operator-issued one-time pairing token (dashboard code);
            the key is bound to the installation created by that redemption.
  challenge: POST /infra/attestation/challenge {installation_id} → single-use nonce (120 s).
  attest  : the installer signs canonical JSON of
            {capabilities, ex5_sha256, installation_id, nonce, terminal_identity, ts}
            (RFC 8785-style: sorted keys, compact separators, UTF-8) and posts it
            with the signature. The server consumes the nonce ATOMICALLY, checks
            ts freshness, key validity (not revoked / expired) and the signature,
            then records the measured hash as `ex5_measured_by = device_signature`.

Algorithms: RSA-PSS-SHA256 (RSA ≥ 2048, .NET RSACng XML or PEM SPKI public key —
Windows PowerShell 5.1 has no Ed25519) and Ed25519 (raw 32-byte key, base64).
"""
import base64
import binascii
import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from pymongo import ReturnDocument

ALGORITHMS = ("RSA-PSS-SHA256", "Ed25519")
NONCE_TTL_S = 120
TS_SKEW_S = 300
KEY_LIFETIME_DAYS = int(os.environ.get("DEVICE_KEY_LIFETIME_DAYS") or 365)
ATTESTATION_MAX_AGE_DAYS = int(os.environ.get("ATTESTATION_MAX_AGE_DAYS") or 30)
SIGNED_FIELDS = ("capabilities", "ex5_sha256", "installation_id", "nonce", "terminal_identity", "ts")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,119}$")
_CAP = re.compile(r"^[a-z0-9_]{1,40}$")


class AttestationError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _now():
    return datetime.now(timezone.utc)


def canonical(payload: dict) -> bytes:
    """Sorted keys, compact separators, UTF-8 — the exact bytes both sides sign."""
    return json.dumps({k: payload[k] for k in SIGNED_FIELDS}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _xml_int(b64: str) -> int:
    return int.from_bytes(base64.b64decode(b64, validate=True), "big", signed=False)   # .NET RSA XML is big-endian


def load_public_key(algorithm: str, value: str):
    try:
        return _load_public_key(algorithm, value)
    except (ET.ParseError, TypeError, binascii.Error, ValueError, AttributeError) as e:
        raise ValueError(f"invalid public key: {type(e).__name__}") from None


def _load_public_key(algorithm: str, value: str):
    if algorithm == "Ed25519":
        raw = base64.b64decode(value, validate=True)
        if len(raw) != 32:
            raise ValueError("Ed25519 public key must be 32 bytes")
        return ed25519.Ed25519PublicKey.from_public_bytes(raw)
    if algorithm == "RSA-PSS-SHA256":
        v = value.strip()
        if v.startswith("-----BEGIN"):
            key = serialization.load_pem_public_key(v.encode())
        else:
            el = ET.fromstring(v)
            if el.tag != "RSAKeyValue":
                raise ValueError("expected RSAKeyValue XML")
            key = rsa.RSAPublicNumbers(_xml_int(el.findtext("Exponent")), _xml_int(el.findtext("Modulus"))).public_key()
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
            raise ValueError("RSA public key must be RSA with ≥ 2048 bits")
        return key
    raise ValueError("unsupported algorithm")


def key_id(algorithm: str, value: str) -> str:
    key = load_public_key(algorithm, value)
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()[:16]


def verify_signature(algorithm: str, public_key_value: str, signature_b64: str, message: bytes) -> None:
    key = load_public_key(algorithm, public_key_value)
    sig = base64.b64decode(signature_b64, validate=True)
    if algorithm == "Ed25519":
        key.verify(sig, message)
    else:
        key.verify(sig, message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                             salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())


def _sign_rsa_pss(private_key, message: bytes) -> str:   # used by tests / reference client
    return base64.b64encode(private_key.sign(
        message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())).decode()


def device_key_record(algorithm: str, public_key: str) -> dict:
    """Validated public-key record to embed on an installation at enrolment."""
    if algorithm not in ALGORITHMS:
        raise ValueError("unsupported algorithm")
    if len(public_key) > 8000:
        raise ValueError("public key too large")
    kid = key_id(algorithm, public_key)
    now = _now()
    return {"algorithm": algorithm, "public_key": public_key, "key_id": kid,
            "enrolled_at": now.isoformat(), "expires_at": (now + timedelta(days=KEY_LIFETIME_DAYS)).isoformat(),
            "revoked": False}


def _key_usable(dk: dict | None) -> str | None:
    """None when the device key can sign for this installation, else a reason code."""
    if not dk:
        return "device_not_enrolled"
    if dk.get("revoked"):
        return "device_key_revoked"
    try:
        if datetime.fromisoformat(dk["expires_at"]) < _now():
            return "device_key_expired"
    except (KeyError, ValueError, TypeError):
        return "device_key_invalid"
    return None


async def ensure_indexes(db):
    await db.attestation_nonces.create_index("expires_at", expireAfterSeconds=0)
    await db.attestation_nonces.create_index("nonce", unique=True)


async def issue_challenge(db, installation_id: str) -> dict:
    inst = await db.installations.find_one({"installation_id": installation_id, "revoked": {"$ne": True}},
                                           {"device_key": 1})
    if not inst:
        raise AttestationError("installation_unknown", "installation not found or revoked", 404)
    why = _key_usable(inst.get("device_key"))
    if why:
        raise AttestationError(why, "no usable enrolled device key for this installation — re-run the installer "
                                    "with a fresh pairing token from the dashboard", 403)
    nonce = secrets.token_urlsafe(32)
    now = _now()
    await db.attestation_nonces.insert_one({"nonce": nonce, "installation_id": installation_id, "used": False,
                                            "issued_at": now, "expires_at": now + timedelta(seconds=NONCE_TTL_S)})
    return {"nonce": nonce, "expires_in": NONCE_TTL_S, "signed_fields": list(SIGNED_FIELDS),
            "key_id": inst["device_key"]["key_id"]}


def _validate_payload(body: dict) -> dict:
    try:
        p = {"installation_id": str(body["installation_id"]), "nonce": str(body["nonce"]),
             "terminal_identity": str(body["terminal_identity"]), "ex5_sha256": str(body["ex5_sha256"]).lower(),
             "capabilities": list(body.get("capabilities") or []), "ts": int(body["ts"])}
    except (KeyError, TypeError, ValueError):
        raise AttestationError("malformed", "installation_id, nonce, terminal_identity, ex5_sha256, ts required", 422)
    if not _HEX64.match(p["ex5_sha256"]):
        raise AttestationError("malformed", "ex5_sha256 must be 64 lowercase hex chars", 422)
    if not _IDENT.match(p["terminal_identity"]):
        raise AttestationError("malformed", "terminal_identity: letters, digits, '.', '_', '-', space (≤120)", 422)
    if len(p["capabilities"]) > 32 or not all(isinstance(c, str) and _CAP.match(c) for c in p["capabilities"]):
        raise AttestationError("malformed", "capabilities: ≤32 tokens of [a-z0-9_]", 422)
    if len(p["nonce"]) > 200 or len(p["installation_id"]) > 64:
        raise AttestationError("malformed", "field too long", 422)
    return p


async def verify_attestation(db, body: dict) -> dict:
    """Consume the nonce and verify the installer signature. Every failure is a 401/4xx —
    never a 500 — and the nonce is burnt on the first attempt regardless of outcome."""
    p = _validate_payload(body)
    signature = str(body.get("signature") or "")
    if not signature or len(signature) > 4096:
        raise AttestationError("invalid_signature", "signature required")
    now = _now()
    if abs(int(now.timestamp()) - p["ts"]) > TS_SKEW_S:
        raise AttestationError("stale_timestamp", f"ts outside ±{TS_SKEW_S}s of server time")
    burnt = await db.attestation_nonces.find_one_and_update(
        {"nonce": p["nonce"], "installation_id": p["installation_id"], "used": False, "expires_at": {"$gt": now}},
        {"$set": {"used": True, "used_at": now}}, return_document=ReturnDocument.AFTER)
    if not burnt:
        raise AttestationError("nonce_invalid", "unknown, expired, replayed or wrong-installation nonce")
    inst = await db.installations.find_one({"installation_id": p["installation_id"], "revoked": {"$ne": True}})
    if not inst:
        raise AttestationError("installation_unknown", "installation not found or revoked")
    dk = inst.get("device_key")
    why = _key_usable(dk)
    if why:
        raise AttestationError(why, "device key not usable")
    try:
        verify_signature(dk["algorithm"], dk["public_key"], signature, canonical(p))
    except (InvalidSignature, ValueError, TypeError):
        await db.attestation_failures.insert_one({"installation_id": p["installation_id"], "key_id": dk["key_id"],
                                                  "at": now.isoformat(), "reason": "invalid_signature"})
        raise AttestationError("invalid_signature", "signature does not verify under the enrolled device key")
    from ea_capabilities import expected_ea_sha256
    expected = (expected_ea_sha256() or "").lower()
    attestation = {"key_id": dk["key_id"], "algorithm": dk["algorithm"], "nonce": p["nonce"], "ts": p["ts"],
                   "terminal_identity": p["terminal_identity"], "capabilities": p["capabilities"],
                   "verified_at": now.isoformat(), "release_match": bool(expected) and expected == p["ex5_sha256"]}
    await db.installations.update_one(
        {"_id": inst["_id"]},
        {"$set": {"ex5_sha256": p["ex5_sha256"], "ex5_sha256_at": now.isoformat(),
                  "ex5_measured_by": "device_signature", "ex5_terminal": p["terminal_identity"],
                  "attestation": attestation}})
    return {"ok": True, "installation_id": p["installation_id"], "key_id": dk["key_id"],
            "release_match": attestation["release_match"], "expected_sha256": expected or None,
            "verified_at": attestation["verified_at"]}


def attested_hash(inst: dict | None) -> tuple[str, str | None]:
    """(method, measured_hash) the heartbeat may rely on:
    installer_attested only for a fresh device-signature attestation."""
    inst = inst or {}
    measured = str(inst.get("ex5_sha256") or "").lower()
    if not measured:
        return "unmeasured", None
    if inst.get("ex5_measured_by") != "device_signature":
        return "installer_unattested", measured          # legacy bridge-token report — telemetry only
    try:
        verified_at = datetime.fromisoformat((inst.get("attestation") or {})["verified_at"])
    except (KeyError, ValueError, TypeError):
        return "installer_unattested", measured
    if _now() - verified_at > timedelta(days=ATTESTATION_MAX_AGE_DAYS):
        return "installer_attestation_stale", measured
    if _key_usable(inst.get("device_key")) == "device_key_revoked":
        return "device_key_revoked", measured
    return "installer_attested", measured
