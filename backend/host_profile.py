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
import json
import os
from datetime import datetime, timezone


HOST_PROFILE_KDF_LABEL = "stoic-host-profile-v1"


def derive_host_profile_key(ledger_anchor_key: str) -> bytes:
    """M115-3 — HKDF-style derivation (extract with a fixed label) so the host-profile HMAC never uses the
    evidence key directly. LIMIT: root on the host can read the anchor key and re-sign a hand-edited
    profile — the signature stops accidental edits and config drift, not a determined administrator."""
    return hmac.new(ledger_anchor_key.encode(), HOST_PROFILE_KDF_LABEL.encode(), hashlib.sha256).digest()


def canonical_payload(profile: str, markers: str, detected_at: str) -> bytes:
    """Audit #15 P3 — length-prefixed fields (`len:value;`) so a `|` inside a marker can never shift fields."""
    return "".join(f"{len(v)}:{v};" for v in (profile, markers, detected_at)).encode()


def _sig(key: str, profile: str, markers: str, detected_at: str) -> str:
    return hmac.new(derive_host_profile_key(key), canonical_payload(profile, markers, detected_at), hashlib.sha256).hexdigest()


MAX_AGE_HOURS = 24.0        # A19-P1-04 — a signed profile older than this is NOT verified
REFRESH_INTERVAL_HOURS = 6.0   # A20-P1-03 — stoic-host-profile.timer cadence (OnUnitActiveSec=6h + ≤10 min jitter)
WARN_AGE_HOURS = 12.0       # A20-P1-03 — older than this ⇒ refresh overdue (≥1 missed run): readiness warns + ops alert
MAX_FUTURE_SKEW_S = 300     # future-dated by more than 5 min of clock skew ⇒ not verified


def _load_file(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def host_profile(env=None, now=None) -> dict:
    """Read on EVERY call (no caching): the daily-refreshed signed file (read-only mount) wins over the
    env snapshot the installer wrote. Expired, future-dated or unparseable ⇒ verified=False with a reason."""
    env = env if env is not None else os.environ
    now = now or datetime.now(timezone.utc)
    source = "env"
    doc = _load_file(env.get("STOIC_HOST_PROFILE_FILE") or "") if env.get("STOIC_HOST_PROFILE_FILE") else None
    if doc is not None:
        source = "file"
        profile = str(doc.get("profile") or "").strip().lower()
        markers = str(doc.get("markers") or "").strip()
        detected_at = str(doc.get("detected_at") or "").strip()
        sig = str(doc.get("sig") or "").strip()
    else:
        profile = (env.get("STOIC_HOST_PROFILE") or "").strip().strip('"').lower()
        markers = (env.get("STOIC_HOST_MARKERS") or "").strip().strip('"')
        detected_at = (env.get("STOIC_HOST_DETECTED_AT") or "").strip().strip('"')
        sig = (env.get("STOIC_HOST_PROFILE_SIG") or "").strip().strip('"')
    key = (env.get("LEDGER_ANCHOR_KEY") or "").strip()
    sig_ok = bool(profile and sig and key) and hmac.compare_digest(sig, _sig(key, profile, markers, detected_at))
    age_h, reason, refresh_overdue = None, None, False
    if not sig_ok:
        reason = "signature missing/invalid" if profile else "no host profile recorded"
    try:
        ts = datetime.fromisoformat(detected_at.replace("Z", "+00:00")) if detected_at else None
        if ts is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        ts = None
    if ts is None:
        reason = reason or "detected_at unparseable"
    else:
        delta_s = (now - ts).total_seconds()
        age_h = round(delta_s / 3600, 1)
        refresh_overdue = delta_s > WARN_AGE_HOURS * 3600
        if delta_s < -MAX_FUTURE_SKEW_S:
            reason = reason or f"detected_at is {round(-delta_s / 60)} min in the future"
        elif delta_s > MAX_AGE_HOURS * 3600:
            reason = reason or f"profile is {age_h} h old (max {MAX_AGE_HOURS:g} h) — stoic-host-profile.timer not running?"
    verified = sig_ok and reason is None
    return {"profile": profile or "unknown", "shared_web_host": profile == "shared-web-host", "markers": markers or None,
            "detected_at": detected_at or None, "age_hours": age_h, "verified": verified, "source": source,
            "unverified_reason": reason, "refresh_overdue": refresh_overdue,
            "refresh_warning": (f"profile is {age_h} h old — refresh overdue (timer runs every {REFRESH_INTERVAL_HOURS:g} h; "
                                f"unverified at {MAX_AGE_HOURS:g} h): check systemctl status stoic-host-profile.timer")
            if refresh_overdue and verified else None}
