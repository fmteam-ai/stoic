"""Ed25519 release signing (iter-136) — replaces HMAC artifact-manifest
signing. Private key comes from the ED25519_SIGNING_KEY_B64 env var
(base64 raw 32 bytes).
Public key is published via GET /api/release-key for verifier pinning.
"""
import base64
import logging
import os

logger = logging.getLogger("release_signing")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

KEY_ID = "stoic-release-ed25519-v1"


def _private_key() -> Ed25519PrivateKey:
    b64 = os.environ.get("ED25519_SIGNING_KEY_B64", "")
    if not b64:
        raise RuntimeError(
            "ED25519_SIGNING_KEY_B64 is not configured — refusing to emit "
            "an UNSIGNED release manifest. Generate one with: python -c "
            "\"import base64; from cryptography.hazmat.primitives.asymmetric"
            ".ed25519 import Ed25519PrivateKey; from cryptography.hazmat."
            "primitives import serialization as s; k=Ed25519PrivateKey."
            "generate(); print(base64.b64encode(k.private_bytes(s.Encoding."
            "Raw, s.PrivateFormat.Raw, s.NoEncryption())).decode())\"")
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(b64))


def sign_hex(data: bytes) -> str:
    """Sign `data` and return a hex Ed25519 signature.

    iter-171 (#1) — pluggable signer so the private key can live OUTSIDE the
    API. RELEASE_SIGNER selects the backend:
      • 'local'    (default) — key from ED25519_SIGNING_KEY_B64 in this process
      • 'external' — POST to an isolated signing service / KMS proxy
                     (RELEASE_SIGNER_URL); the private key never touches the API.
    """
    mode = os.environ.get("RELEASE_SIGNER", "local").strip().lower()
    if mode == "external":
        return _external_sign(data)
    from app_env import is_production
    if is_production():
        # audit v5 P0-7 — local signing in production requires the SAME
        # explicit acknowledgment as the boot guard (kept consistent so
        # an acknowledged pilot never hits a post-boot signing failure).
        ack = os.environ.get("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD",
                             "false").strip().lower() == "true"
        if not ack:
            raise RuntimeError(
                "RELEASE_SIGNER=local is forbidden in production — the "
                "private signing key must NOT live in the API. Configure "
                "RELEASE_SIGNER=external + RELEASE_SIGNER_URL/RELEASE_"
                "SIGNER_TOKEN (KMS/HSM proxy), or explicitly acknowledge "
                "with RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD=true for a "
                "supervised pilot.")
        logger.warning("release signing with LOCAL key in production "
                       "(explicitly acknowledged) — migrate to KMS/HSM")
    return _private_key().sign(data).hex()


def signer_status() -> dict:
    """Operational visibility for the release-signing backend (#1 KMS)."""
    mode = os.environ.get("RELEASE_SIGNER", "local").strip().lower()
    return {"mode": mode,
            "external_configured": bool(
                os.environ.get("RELEASE_SIGNER_URL")
                and os.environ.get("RELEASE_SIGNER_TOKEN")),
            "public_key_pinned": bool(
                os.environ.get("RELEASE_PUBLIC_KEY_B64")),
            "key_id": KEY_ID}


def _external_sign(data: bytes) -> str:
    """Delegate signing to an isolated service. Contract:
    POST {url}/sign  Authorization: Bearer <RELEASE_SIGNER_TOKEN>
    body {"key_id": KEY_ID, "data_hex": "<hex>"} → {"signature_hex": "<hex>"}."""
    import requests
    url = os.environ.get("RELEASE_SIGNER_URL")
    token = os.environ.get("RELEASE_SIGNER_TOKEN")
    if not url or not token:
        raise RuntimeError(
            "RELEASE_SIGNER=external requires RELEASE_SIGNER_URL and "
            "RELEASE_SIGNER_TOKEN (the API must NOT hold the private key)")
    r = requests.post(
        f"{url.rstrip('/')}/sign",
        json={"key_id": KEY_ID, "data_hex": data.hex()},
        headers={"Authorization": f"Bearer {token}"},
        timeout=float(os.environ.get("RELEASE_SIGNER_TIMEOUT", "10")))
    r.raise_for_status()
    sig = r.json().get("signature_hex")
    if not sig:
        raise RuntimeError("external signer returned no signature_hex")
    # defence-in-depth: verify the returned signature against the pinned pubkey
    if not verify_hex(data, sig):
        raise RuntimeError("external signer signature failed local verification")
    return sig


def public_key_b64() -> str:
    # In external mode the API never sees the private key; the public key is
    # provided out-of-band via RELEASE_PUBLIC_KEY_B64 (or the signer service).
    pinned = os.environ.get("RELEASE_PUBLIC_KEY_B64")
    if pinned:
        return pinned.strip()
    pub = _private_key().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(pub).decode()


def verify_hex(data: bytes, signature_hex: str,
               pub_b64: str | None = None) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(pub_b64 or public_key_b64()))
        pub.verify(bytes.fromhex(signature_hex), data)
        return True
    except Exception:  # noqa: BLE001
        return False
