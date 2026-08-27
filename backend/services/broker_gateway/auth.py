"""Broker → STOIC webhook security (Phase 16): HMAC signatures, ±300s replay
window, idempotency via unique event keys."""
import hashlib
import hmac
import time

REPLAY_WINDOW_SEC = 300


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body,
                    hashlib.sha256).hexdigest()


def verify_webhook(secret: str, timestamp: str, signature: str,
                   body: bytes) -> tuple[bool, str]:
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        return False, "invalid timestamp"
    if abs(time.time() - ts) > REPLAY_WINDOW_SEC:
        return False, "timestamp outside replay window"
    expected = sign_payload(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature or ""):
        return False, "bad signature"
    return True, "ok"
