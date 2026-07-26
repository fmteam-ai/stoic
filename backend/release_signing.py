"""Ed25519 release signing (iter-136) — replaces HMAC artifact-manifest
signing. Private key: ED25519_SIGNING_KEY_B64 (base64 raw 32 bytes).
Public key is published via GET /api/release-key for verifier pinning.
"""
import base64
import os

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
    return _private_key().sign(data).hex()


def public_key_b64() -> str:
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
