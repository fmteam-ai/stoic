"""M120-2 — key/certificate age reminders. The host writes deploy/state/key_ages.json (deploy/preflight.sh
write_key_ages_file — installer, update, stoic-host-profile.timer); the container reads it (ro mount) and
WARNS when the CI release key or the runtime (sidecar) key is older than 180 days, or the Cloudflare Origin CA
certificate expires within 30 days. Advisory: never blocks, but raises a `key_rotation_due` ops alert."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

KEY_MAX_AGE_DAYS = 180
CERT_WARN_DAYS = 30

FIXES = {
    "release_key": "docs/RELEASE_KEY_ROTATION.md (deploy/signer/rotate_fly_signer_key.sh → deploy/rotate-release-pin.sh)",
    "runtime_key": "sudo bash deploy/rotate-runtime-key.sh",
    "origin_cert": "Cloudflare → SSL/TLS → Origin Server → create a new certificate; replace secrets/origin_cert.pem + "
                   "secrets/origin_key.pem; sudo bash deploy/restart.sh caddy",
}


def _path(env) -> str:
    p = env.get("STOIC_KEY_AGES_FILE")
    if p:
        return p
    hp = env.get("STOIC_HOST_PROFILE_FILE")
    return os.path.join(os.path.dirname(hp), "key_ages.json") if hp else "deploy/state/key_ages.json"


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc) if s else None
    except ValueError:
        return None


def _days(a: datetime, b: datetime) -> int:
    return int((a - b).total_seconds() // 86400)


def plan(doc: dict | None, now: datetime) -> dict:
    """Pure evaluation of a key_ages.json document."""
    if not doc:
        return {"available": False, "ok": True, "due": [], "items": [], "warning": None, "generated_at": None}
    items = []
    rk = doc.get("release_key") or {}
    created = _ts(rk.get("created"))
    age = _days(now, created) if created else None
    items.append({"name": "release_key", "kind": "key", "label": f"CI release key {rk.get('key_id') or '?'}",
                  "created": rk.get("created"), "age_days": age, "limit_days": KEY_MAX_AGE_DAYS,
                  "due": age is not None and age > KEY_MAX_AGE_DAYS,
                  "note": None if created else "no created= date in release/release_key.fingerprint"})
    rt = doc.get("runtime_key") or {}
    created = _ts(rt.get("created"))
    age = _days(now, created) if created else None
    items.append({"name": "runtime_key", "kind": "key", "label": "runtime (sidecar) signing key", "created": rt.get("created"),
                  "age_days": age, "limit_days": KEY_MAX_AGE_DAYS, "due": age is not None and age > KEY_MAX_AGE_DAYS,
                  "note": None if created else f"{rt.get('path') or 'secrets/signer_ed25519_key'} not found on the host"})
    oc = doc.get("origin_cert") or {}
    if oc.get("present"):
        na = _ts(oc.get("not_after"))
        left = _days(na, now) if na else None
        items.append({"name": "origin_cert", "kind": "cert", "label": "Cloudflare Origin CA certificate", "not_after": oc.get("not_after"),
                      "days_left": left, "limit_days": CERT_WARN_DAYS, "due": left is not None and left < CERT_WARN_DAYS,
                      "note": None if na else "notAfter unparseable"})
    for it in items:
        it["fix"] = FIXES[it["name"]] if it["due"] else None
    due = [it for it in items if it["due"]]
    parts = []
    for it in due:
        if it["kind"] == "key":
            parts.append(f"{it['label']} is {it['age_days']} days old (rotate every {KEY_MAX_AGE_DAYS} d)")
        else:
            parts.append(f"{it['label']} expires in {it['days_left']} days" if it["days_left"] >= 0
                         else f"{it['label']} EXPIRED {-it['days_left']} days ago")
    return {"available": True, "ok": True, "due": [it["name"] for it in due], "items": items,
            "warning": "; ".join(parts) or None, "generated_at": doc.get("generated_at"),
            "stale": _days(now, _ts(doc.get("generated_at")) or now) > 2}


def load(env=None) -> dict | None:
    env = env if env is not None else os.environ
    try:
        with open(_path(env)) as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def key_ages(env=None, now=None) -> dict:
    return plan(load(env), now or datetime.now(timezone.utc))
