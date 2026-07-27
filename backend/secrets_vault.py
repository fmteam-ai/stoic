"""AES-256-GCM secrets vault for at-rest encryption of broker API keys.

Used for storing third-party exchange credentials (Binance, OKX, Bybit, etc.)
when those integrations land. The master key MUST come from env (KEY_VAULT_MASTER)
or a secrets manager (HashiCorp Vault / AWS Secrets Manager) — NEVER hard-coded.

Storage shape (in MongoDB):
    {
        "ciphertext": "<base64>",   # AES-GCM ciphertext (includes auth tag)
        "nonce":      "<base64>",   # 12-byte random nonce per encryption
        "v":          1,            # version, for future key rotation
    }

Decryption only happens inside the bot worker the moment a broker session is
opened, and the plaintext is never written to disk or logs.
"""
import os
import base64
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes


def _derive_key_from_master(material: str) -> bytes:
    """HKDF-derive a 32-byte AES key from any-length master string.

    Lets the operator paste a passphrase or pass a 32-byte hex value.
    Salt is deterministic per app so the same master always yields the same key.
    """
    if not material:
        raise RuntimeError("KEY_VAULT_MASTER not set — cannot encrypt/decrypt secrets")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"emergent-trading-bot-v1",
        info=b"vault-key",
    )
    return hkdf.derive(material.encode("utf-8"))


def _aesgcm() -> AESGCM:
    # SEC-002 — the broker-credential vault MUST have its own key so a leaked
    # JWT_SECRET can never both forge auth tokens AND decrypt broker
    # passwords. Production refuses to fall back; dev derives a deterministic
    # key from JWT_SECRET only so the bot boots locally.
    master = os.environ.get("KEY_VAULT_MASTER")
    if not master:
        from app_env import is_production
        is_prod = is_production()
        if is_prod:
            raise RuntimeError(
                "KEY_VAULT_MASTER is required in production (must be distinct "
                "from JWT_SECRET) — refusing to encrypt broker credentials "
                "under the auth-signing secret.")
        master = os.environ.get("JWT_SECRET", "dev-fallback-master")
    return AESGCM(_derive_key_from_master(master))


def encrypt(plaintext: str, associated_data: bytes = b"") -> dict:
    """Encrypt a string and return a JSON-storable dict."""
    if plaintext is None:
        raise ValueError("plaintext is required")
    nonce = secrets.token_bytes(12)
    ct = _aesgcm().encrypt(nonce, plaintext.encode("utf-8"), associated_data or None)
    return {
        "ciphertext": base64.b64encode(ct).decode("ascii"),
        "nonce":      base64.b64encode(nonce).decode("ascii"),
        "v": 1,
    }


def decrypt(blob: dict, associated_data: bytes = b"") -> str:
    """Reverse of encrypt(). Raises on tampering or wrong master key."""
    if not blob or "ciphertext" not in blob or "nonce" not in blob:
        raise ValueError("bad ciphertext blob")
    ct = base64.b64decode(blob["ciphertext"])
    nonce = base64.b64decode(blob["nonce"])
    pt = _aesgcm().decrypt(nonce, ct, associated_data or None)
    return pt.decode("utf-8")


def mask(value: str, keep: int = 4) -> str:
    """Display-safe masked value, never the real secret."""
    if not value:
        return ""
    if len(value) <= keep:
        return "•" * len(value)
    return f"{value[:keep]}{'•' * (len(value) - keep)}"
