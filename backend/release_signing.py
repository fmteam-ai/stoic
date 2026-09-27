"""Ed25519 release signing — ONE complete signer-configuration validator
shared by server boot, deploy_preflight, sign_hex and the CI canary
(audit round 9 P1-01).

Modes (RELEASE_SIGNER):
  external  — POST {RELEASE_SIGNER_URL}/sign; the private key never touches the
              API. Requires HTTPS URL whose host is in RELEASE_SIGNER_ALLOWED_HOSTS,
              RELEASE_SIGNER_TOKEN, pinned RELEASE_PUBLIC_KEY_B64, RELEASE_SIGNER_KEY_ID,
              sane RELEASE_SIGNER_TIMEOUT (1–30 s). ED25519_SIGNING_KEY_B64 is FORBIDDEN.
              The only mode allowed in production.
  local     — ED25519_SIGNING_KEY_B64 in-process (non-production only); if
              RELEASE_PUBLIC_KEY_B64 is also set it must match the derived key.
"""
import base64
import logging
import os
from urllib.parse import urlparse

logger = logging.getLogger("release_signing")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

KEY_ID = "stoic-release-ed25519-v1"
DEFAULT_TIMEOUT = 10.0


def _mode(env) -> str:
    return (env.get("RELEASE_SIGNER") or "local").strip().lower()


def _is_prod(env) -> bool:
    return (env.get("APP_ENV") or "").strip().lower() in ("production", "prod")   # mirrors app_env._PROD_VALUES


def _b64_key_ok(b64: str, length: int = 32) -> bool:
    try:
        return len(base64.b64decode(b64, validate=True)) == length
    except Exception:  # noqa: BLE001
        return False


def key_id(env=None) -> str:
    env = env if env is not None else os.environ
    return (env.get("RELEASE_SIGNER_KEY_ID") or KEY_ID).strip()


def _timeout(env) -> float | None:
    raw = (env.get("RELEASE_SIGNER_TIMEOUT") or str(DEFAULT_TIMEOUT)).strip()
    try:
        t = float(raw)
    except ValueError:
        return None
    return t if 1.0 <= t <= 30.0 else None


def signer_config_violations(env) -> list[str]:
    """Complete validator. Empty list ⇒ configuration is coherent for its mode."""
    v: list[str] = []
    mode = _mode(env)
    prod = _is_prod(env)
    if mode not in ("local", "external"):
        return [f"RELEASE_SIGNER={mode!r} is not a known mode (local|external)"]
    retired = (env.get("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD") or "").strip().lower() == "true"
    retired_msg = " RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD is RETIRED and has no effect — remove it."
    if mode == "local":
        if prod:
            return ["APP_ENV=production forbids RELEASE_SIGNER=local — the signing key must not live in the API. "
                    "Configure RELEASE_SIGNER=external with RELEASE_SIGNER_URL/RELEASE_SIGNER_TOKEN (KMS/HSM-backed)."
                    + (retired_msg if retired else "")]
        if retired:
            v.append(retired_msg.strip())
        priv = (env.get("ED25519_SIGNING_KEY_B64") or "").strip()
        if not _b64_key_ok(priv):
            v.append("RELEASE_SIGNER=local requires a valid ED25519_SIGNING_KEY_B64 (base64 of 32 raw bytes).")
        pinned = (env.get("RELEASE_PUBLIC_KEY_B64") or "").strip()
        if priv and pinned and _b64_key_ok(priv):
            derived = base64.b64encode(Ed25519PrivateKey.from_private_bytes(base64.b64decode(priv)).public_key()
                                       .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
            if derived != pinned:
                v.append("RELEASE_PUBLIC_KEY_B64 does not match the public key derived from ED25519_SIGNING_KEY_B64.")
        return v
    # external
    if retired:
        v.append(retired_msg.strip())
    if (env.get("ED25519_SIGNING_KEY_B64") or "").strip():
        v.append("RELEASE_SIGNER=external forbids ED25519_SIGNING_KEY_B64 in the API environment — remove the private key.")
    url = (env.get("RELEASE_SIGNER_URL") or "").strip()
    if not url:
        v.append("RELEASE_SIGNER=external requires RELEASE_SIGNER_URL.")
    else:
        p = urlparse(url)
        if p.scheme != "https":
            v.append("RELEASE_SIGNER_URL must use https.")
        allowed = {h.strip().lower() for h in (env.get("RELEASE_SIGNER_ALLOWED_HOSTS") or "").split(",") if h.strip()}
        if not allowed:
            if prod:
                v.append("RELEASE_SIGNER_ALLOWED_HOSTS is required in production (comma list of permitted signer hosts).")
        elif (p.hostname or "").lower() not in allowed:
            v.append(f"RELEASE_SIGNER_URL host {p.hostname!r} is not in RELEASE_SIGNER_ALLOWED_HOSTS.")
    if not (env.get("RELEASE_SIGNER_TOKEN") or "").strip():
        v.append("RELEASE_SIGNER=external requires RELEASE_SIGNER_TOKEN (credential reference; *_FILE supported).")
    pinned = (env.get("RELEASE_PUBLIC_KEY_B64") or "").strip()
    if not _b64_key_ok(pinned):
        v.append("RELEASE_SIGNER=external requires a pinned, valid RELEASE_PUBLIC_KEY_B64 (base64 of 32 raw bytes).")
    if not (env.get("RELEASE_SIGNER_KEY_ID") or "").strip():
        v.append("RELEASE_SIGNER=external requires RELEASE_SIGNER_KEY_ID.")
    if _timeout(env) is None:
        v.append("RELEASE_SIGNER_TIMEOUT must be a number of seconds between 1 and 30.")
    return v


def production_signer_violation(env) -> str | None:
    """Boot/preflight entry point: first violation, or None when coherent.
    In production the mode must be external AND its configuration complete."""
    v = signer_config_violations(env)
    return v[0] if v else None


def _private_key() -> Ed25519PrivateKey:
    b64 = os.environ.get("ED25519_SIGNING_KEY_B64", "")
    if not b64:
        raise RuntimeError(
            "ED25519_SIGNING_KEY_B64 is not configured — refusing to emit an UNSIGNED release manifest.")
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(b64))


def sign_hex(data: bytes) -> str:
    """Sign `data` → hex Ed25519 signature via the configured backend.
    Refuses to sign with an incoherent configuration (same validator as boot)."""
    env = os.environ
    if _mode(env) == "external":
        return _external_sign(data)
    if _is_prod(env):
        viol = production_signer_violation(env)
        if viol:
            raise RuntimeError(viol)
    return _private_key().sign(data).hex()


def signer_status() -> dict:
    env = os.environ
    return {"mode": _mode(env),
            "external_configured": bool(env.get("RELEASE_SIGNER_URL") and env.get("RELEASE_SIGNER_TOKEN")),
            "public_key_pinned": bool(env.get("RELEASE_PUBLIC_KEY_B64")),
            "key_id": key_id(env),
            "config_violations": len(signer_config_violations(env))}


def _external_sign(data: bytes) -> str:
    """POST {url}/sign  Authorization: Bearer <token>
    body {"key_id", "data_hex"} → {"signature_hex", "key_id"}. Fail closed on
    any config, transport, shape, key-id or signature-verification problem."""
    import requests
    env = os.environ
    viol = signer_config_violations(env)
    if viol:
        raise RuntimeError("external signer configuration invalid: " + viol[0])
    url = env["RELEASE_SIGNER_URL"].strip().rstrip("/")
    kid = key_id(env)
    try:
        r = requests.post(f"{url}/sign", json={"key_id": kid, "data_hex": data.hex()},
                          headers={"Authorization": f"Bearer {env['RELEASE_SIGNER_TOKEN'].strip()}"},
                          timeout=_timeout(env))
        r.raise_for_status()
        body = r.json()
    except requests.RequestException as e:
        raise RuntimeError(f"external signer unavailable: {type(e).__name__}") from e
    except ValueError as e:
        raise RuntimeError("external signer returned a malformed (non-JSON) response") from e
    if not isinstance(body, dict):
        raise RuntimeError("external signer returned a malformed response")
    if body.get("key_id") not in (None, kid):
        raise RuntimeError(f"external signer answered for key_id {body.get('key_id')!r}, expected {kid!r}")
    sig = body.get("signature_hex")
    if not isinstance(sig, str) or len(sig) != 128:
        raise RuntimeError("external signer returned no/invalid signature_hex")
    if not verify_hex(data, sig, env["RELEASE_PUBLIC_KEY_B64"].strip()):
        raise RuntimeError("external signer signature failed local verification against the pinned public key")
    return sig


def signer_health(env=None) -> dict:
    """Non-signing connectivity/identity check: GET {url}/health must answer
    {ok:true, key_id, public_key_b64} matching the pinned identity."""
    import requests
    env = env if env is not None else os.environ
    out = {"mode": _mode(env), "ok": False, "key_id": key_id(env)}
    viol = signer_config_violations(env)
    if viol:
        out["error"] = viol[0]
        return out
    if _mode(env) != "external":
        out.update(ok=True, note="local signer — no remote identity to check")
        return out
    try:
        r = requests.get(f"{env['RELEASE_SIGNER_URL'].strip().rstrip('/')}/health",
                         headers={"Authorization": f"Bearer {env['RELEASE_SIGNER_TOKEN'].strip()}"},
                         timeout=_timeout(env))
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        out["error"] = f"signer unreachable/malformed: {type(e).__name__}"
        return out
    out["remote_key_id"] = body.get("key_id")
    out["identity_matches"] = (body.get("key_id") == key_id(env)
                               and (body.get("public_key_b64") or "").strip() == env["RELEASE_PUBLIC_KEY_B64"].strip())
    out["ok"] = bool(body.get("ok")) and out["identity_matches"]
    if not out["ok"]:
        out["error"] = "signer identity mismatch" if not out["identity_matches"] else "signer reports not ok"
    return out


def public_key_b64() -> str:
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
