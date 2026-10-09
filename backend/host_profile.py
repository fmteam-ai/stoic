"""M114-7 — host suitability profile, verified at read time.

The backend runs in a container and cannot see cPanel/Plesk/httpd markers itself, so the installer /
update preflight (deploy/preflight.sh record_host_profile) detects them on the host and records
    STOIC_HOST_PROFILE   shared-web-host | dedicated
    STOIC_HOST_MARKERS   "cPanel/WHM httpd exim"
    STOIC_HOST_DETECTED_AT   ISO timestamp
    STOIC_HOST_PROFILE_SIG   HMAC-SHA256(ledger anchor key, "profile|markers|detected_at")
A hand-edited `dedicated` without the matching signature is reported as UNVERIFIED (fails closed in
production). The signature key is the dedicated evidence key the preflight can read from secrets/."""
import hashlib
import hmac
import os
from datetime import datetime, timezone


HOST_PROFILE_KDF_LABEL = "stoic-host-profile-v1"


def derive_host_profile_key(ledger_anchor_key: str) -> bytes:
    """M115-3 — HKDF-style derivation (extract with a fixed label) so the host-profile HMAC never uses the
    evidence key directly. LIMIT: root on the host can read the anchor key and re-sign a hand-edited
    profile — the signature stops accidental edits and config drift, not a determined administrator."""
    return hmac.new(ledger_anchor_key.encode(), HOST_PROFILE_KDF_LABEL.encode(), hashlib.sha256).digest()


def _sig(key: str, profile: str, markers: str, detected_at: str) -> str:
    return hmac.new(derive_host_profile_key(key), "|".join((profile, markers, detected_at)).encode(), hashlib.sha256).hexdigest()


def host_profile(env=None) -> dict:
    env = env if env is not None else os.environ
    profile = (env.get("STOIC_HOST_PROFILE") or "").strip().strip('"').lower()
    markers = (env.get("STOIC_HOST_MARKERS") or "").strip().strip('"')
    detected_at = (env.get("STOIC_HOST_DETECTED_AT") or "").strip().strip('"')
    sig = (env.get("STOIC_HOST_PROFILE_SIG") or "").strip().strip('"')
    key = (env.get("LEDGER_ANCHOR_KEY") or "").strip()
    verified = bool(profile and sig and key) and hmac.compare_digest(sig, _sig(key, profile, markers, detected_at))
    age_h = None
    if detected_at:
        try:
            age_h = round((datetime.now(timezone.utc) - datetime.fromisoformat(detected_at.replace("Z", "+00:00"))).total_seconds() / 3600, 1)
        except ValueError:
            age_h = None
    return {"profile": profile or "unknown", "shared_web_host": profile == "shared-web-host", "markers": markers or None,
            "detected_at": detected_at or None, "age_hours": age_h, "verified": verified}
