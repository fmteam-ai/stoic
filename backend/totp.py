"""TOTP 2FA helpers — provisioning URI, QR code, code verification, recovery codes.

Uses pyotp + qrcode[pil]. Persists `totp_secret`, `two_factor_enabled`,
and `recovery_codes` (bcrypt-hashed) on the user document. Recovery codes
are one-shot — once consumed they are removed from the user record.
"""
import base64
import io
import secrets

import bcrypt
import pyotp
import qrcode

TOTP_ISSUER = "STOIC Trading Bot"
RECOVERY_CODES_COUNT = 8


def new_secret() -> str:
    """Generate a base32 TOTP secret."""
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    """Build the otpauth:// URI scanned by Google Authenticator / Authy."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=TOTP_ISSUER)


def qr_png_data_url(uri: str) -> str:
    """Render the provisioning URI as a base64 PNG data URL."""
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


def verify_code(secret: str, code: str) -> bool:
    """Constant-time verify of the 6-digit TOTP code with ±1 step tolerance."""
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)
    except Exception:
        return False


def generate_recovery_codes(n: int = RECOVERY_CODES_COUNT) -> list[str]:
    """Return n human-friendly 10-char hex recovery codes."""
    return [secrets.token_hex(5).upper() for _ in range(n)]


def hash_recovery_codes(codes: list[str]) -> list[str]:
    """Bcrypt-hash recovery codes so the DB never holds plaintext."""
    return [bcrypt.hashpw(c.encode(), bcrypt.gensalt()).decode() for c in codes]


def consume_recovery_code(stored_hashes: list[str], code: str) -> tuple[bool, list[str]]:
    """Try to consume a recovery code. Returns (matched, remaining_hashes)."""
    if not code:
        return (False, stored_hashes)
    code_b = code.strip().upper().encode()
    remaining = []
    matched = False
    for h in stored_hashes:
        try:
            if not matched and bcrypt.checkpw(code_b, h.encode()):
                matched = True
                continue  # drop matched hash
        except Exception:
            pass
        remaining.append(h)
    return matched, remaining
