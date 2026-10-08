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

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)

from app_env import removable_secret

logger = logging.getLogger("release_signing")

KEY_ID = "stoic-release-ed25519-v1"          # CI release key: EA records, model manifests, policy migrations
BUNDLE_KEY_ID = "stoic-bundle-ed25519-v1"    # N101-5 — runtime (sidecar) key: bundles, anchors, artifact manifests
DEFAULT_TIMEOUT = 10.0

# N100-11 — domain separation. Every signature is over `<domain>\0<data>`, so a signature minted
# for one purpose can never verify as another, and the signer enforces WHICH token may request
# WHICH purpose: the API only ever holds the bundle token, CI holds the release token.
PURPOSES = {
    "ea-release":        b"stoic:ea-release:v1\0",        # CI only (release token)
    "model-manifest":    b"stoic:model-manifest:v1\0",    # CI only (release token)
    "policy-migration":  b"stoic:policy-migration:v1\0",  # operator/CI (release token) — inventory policy transitions
    "acceptance-bundle": b"stoic:acceptance-bundle:v1\0", # API (bundle token)
    "artifact-manifest": b"stoic:artifact-manifest:v1\0", # API (bundle token) — N101-5 EA/agent artifact manifests
    "audit-anchor":      b"stoic:audit-anchor:v1\0",      # API
    "differentiation":   b"stoic:differentiation:v1\0",   # API
    "canary":            b"stoic:canary:v1\0",            # API (chaos drill round-trip)
}
RELEASE_PURPOSES = {"ea-release", "model-manifest", "policy-migration"}
API_PURPOSES = set(PURPOSES) - RELEASE_PURPOSES   # what a running trading API may ask the signer for


class SignerDeferred(RuntimeError):
    """Release signing is explicitly deferred — nothing may be signed."""


DEFERRED_MESSAGE = ("release signer DEFERRED (RELEASE_SIGNER_DEFERRED=true): the hosted signer is not "
                    "provisioned yet — nothing is signed and live capital exposure stays CLOSE_ONLY "
                    "until RELEASE_SIGNER=external is fully configured.")


def _deferred(env) -> bool:
    """Explicit operator declaration. The flag is a NEW key (flows in from .env
    when the publish Secrets panel refuses edits to existing keys) and is
    honoured ONLY in production; preview keeps its local test signer."""
    if (env.get("RELEASE_SIGNER") or "").strip().lower() == "external":
        return False        # a configured external signer always wins over the deferral flag
    return _is_prod(env) and (env.get("RELEASE_SIGNER_DEFERRED") or "").strip().lower() == "true"


def _mode(env) -> str:
    if _deferred(env) or (env.get("RELEASE_SIGNER") or "").strip().lower() == "deferred":
        return "deferred"
    return (env.get("RELEASE_SIGNER") or "local").strip().lower()


def is_deferred(env=None) -> bool:
    return _mode(os.environ if env is None else env) == "deferred"


def deferred_gate(env=None) -> dict | None:
    """Authority gate: while signing is deferred no NEW live exposure is admitted."""
    if is_deferred(env):
        return {"code": "RELEASE_SIGNER_DEFERRED",
                "reason": "release signing is deferred — hosted signer not provisioned; new live exposure is closed"}
    return None


def _is_prod(env) -> bool:
    return (env.get("APP_ENV") or "").strip().lower() in ("production", "prod")   # mirrors app_env._PROD_VALUES


def _b64_key_ok(b64: str, length: int = 32) -> bool:
    try:
        return len(base64.b64decode(b64, validate=True)) == length
    except Exception:  # noqa: BLE001
        return False


def key_id(env=None, purpose: str = "ea-release") -> str:
    """Key id that must sign `purpose`: runtime purposes use the bundle key (BUNDLE_SIGNER_KEY_ID),
    release purposes the CI release key. A single-key install leaves BUNDLE_* unset (same key)."""
    env = env if env is not None else os.environ
    if purpose in API_PURPOSES and (env.get("BUNDLE_SIGNER_KEY_ID") or "").strip():
        return env["BUNDLE_SIGNER_KEY_ID"].strip()
    return (env.get("RELEASE_SIGNER_KEY_ID") or KEY_ID).strip()


def _pinned_pub(env, purpose: str) -> str:
    """Pinned public key for `purpose` (N101-5: the CI release key and the runtime bundle key are pinned separately)."""
    if purpose in API_PURPOSES and (env.get("BUNDLE_PUBLIC_KEY_B64") or "").strip():
        return env["BUNDLE_PUBLIC_KEY_B64"].strip()
    return (env.get("RELEASE_PUBLIC_KEY_B64") or "").strip()


def _tls_verify(env):
    """System trust store by default; RELEASE_SIGNER_CA_BUNDLE pins a private CA /
    self-signed certificate (docker sidecar) — never disables verification."""
    bundle = (env.get("RELEASE_SIGNER_CA_BUNDLE") or "").strip()
    return bundle if bundle else True


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
    if mode not in ("local", "external", "deferred"):
        return [f"RELEASE_SIGNER={mode!r} is not a known mode (local|external|deferred)"]
    retired = (env.get("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD") or "").strip().lower() == "true"
    retired_msg = " RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD is RETIRED and has no effect — remove it."
    if mode == "deferred":
        # coherent only while the private key is ABSENT — deferred never means "sign locally"
        if removable_secret(env, "ED25519_SIGNING_KEY_B64"):
            v.append("RELEASE_SIGNER deferred forbids ED25519_SIGNING_KEY_B64 in the API environment — "
                     "remove the private key (PRODUCTION_RETIRED_SECRETS or `disabled`).")
        return v
    if mode == "local":
        if prod:
            return ["APP_ENV=production forbids RELEASE_SIGNER=local — the signing key must not live in the API. "
                    "Configure RELEASE_SIGNER=external with RELEASE_SIGNER_URL/RELEASE_SIGNER_TOKEN (KMS/HSM-backed)."
                    + (retired_msg if retired else "")]
        if retired:
            v.append(retired_msg.strip())
        priv = removable_secret(env, "ED25519_SIGNING_KEY_B64")
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
    if removable_secret(env, "ED25519_SIGNING_KEY_B64"):
        v.append("RELEASE_SIGNER=external forbids ED25519_SIGNING_KEY_B64 in the API environment — "
                 "remove the private key (set it to `disabled` if the Secrets panel refuses an empty value).")
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
    if not ((env.get("RELEASE_SIGNER_TOKEN") or "").strip() or (env.get("RELEASE_SIGNER_BUNDLE_TOKEN") or "").strip()):
        v.append("RELEASE_SIGNER=external requires RELEASE_SIGNER_BUNDLE_TOKEN (API host) or RELEASE_SIGNER_TOKEN (CI only); *_FILE supported.")
    pinned = (env.get("RELEASE_PUBLIC_KEY_B64") or "").strip()
    bpinned = (env.get("BUNDLE_PUBLIC_KEY_B64") or "").strip()
    if pinned and not _b64_key_ok(pinned):
        v.append("RELEASE_PUBLIC_KEY_B64 is set but is not a valid Ed25519 public key (base64 of 32 raw bytes).")
    if bpinned and not _b64_key_ok(bpinned):
        v.append("BUNDLE_PUBLIC_KEY_B64 is set but is not a valid Ed25519 public key (base64 of 32 raw bytes).")
    if not (_b64_key_ok(pinned) or _b64_key_ok(bpinned)):
        # N102-5 — a host that only signs runtime artefacts needs the bundle pin; the CI release pin is
        # for VERIFYING EA records / model manifests and may be absent (they then simply do not verify).
        v.append("RELEASE_SIGNER=external requires a pinned, valid public key: BUNDLE_PUBLIC_KEY_B64 (runtime) "
                 "and/or RELEASE_PUBLIC_KEY_B64 (CI release key).")
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
    b64 = removable_secret(os.environ, "ED25519_SIGNING_KEY_B64")
    if not b64:
        raise RuntimeError(
            "ED25519_SIGNING_KEY_B64 is not configured — refusing to emit an UNSIGNED release manifest.")
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(b64))


def domain_bytes(purpose, data: bytes) -> bytes:
    if purpose is None:            # raw/legacy: externally produced signatures (broker statements) and pre-N100-11 anchors
        return data
    try:
        return PURPOSES[purpose] + data
    except KeyError:
        raise ValueError(f"unknown signing purpose {purpose!r}")


def _token_for(purpose: str, env) -> str:
    """Per-purpose bearer tokens, no fallback (N101-5): runtime purposes need the bundle token the API
    holds; release purposes need the CI release token. A missing token fails closed."""
    if purpose in API_PURPOSES:
        tok = (env.get("RELEASE_SIGNER_BUNDLE_TOKEN") or "").strip()
        if not tok:
            raise RuntimeError(f"runtime purpose {purpose!r} requires RELEASE_SIGNER_BUNDLE_TOKEN — "
                               "the release token never signs runtime artefacts")
        return tok
    tok = (env.get("RELEASE_SIGNER_TOKEN") or "").strip()
    if not tok:
        raise RuntimeError(f"release purpose {purpose!r} requires RELEASE_SIGNER_TOKEN (CI only)")
    return tok


def sign_hex(data: bytes, purpose: str) -> str:
    """Sign `data` (domain-prefixed by `purpose`, REQUIRED) → hex Ed25519 signature via the configured backend.
    Refuses to sign with an incoherent configuration (same validator as boot)."""
    env = os.environ
    msg = domain_bytes(purpose, data)
    if purpose is None:
        raise ValueError("signing purpose is required")
    if _mode(env) == "deferred":
        raise SignerDeferred(DEFERRED_MESSAGE)
    if _mode(env) == "external":
        return _external_sign(data, purpose)
    if _is_prod(env):
        viol = production_signer_violation(env)
        if viol:
            raise RuntimeError(viol)
    return _private_key().sign(msg).hex()


def signer_status() -> dict:
    env = os.environ
    return {"mode": _mode(env),
            "deferred": _mode(env) == "deferred",
            "external_configured": bool(env.get("RELEASE_SIGNER_URL")
                                        and (env.get("RELEASE_SIGNER_BUNDLE_TOKEN") or env.get("RELEASE_SIGNER_TOKEN"))),
            "public_key_pinned": bool(env.get("RELEASE_PUBLIC_KEY_B64")),
            "bundle_key_pinned": bool(env.get("BUNDLE_PUBLIC_KEY_B64")),
            "key_id": key_id(env),
            "bundle_key_id": key_id(env, "acceptance-bundle"),
            "release_token_present": bool(removable_secret(env, "RELEASE_SIGNER_TOKEN")),
            "config_violations": len(signer_config_violations(env))}


def _external_sign(data: bytes, purpose: str) -> str:
    """POST {url}/sign  Authorization: Bearer <token>
    body {"key_id", "data_hex", "purpose"} → {"signature_hex", "key_id"}. Fail closed on
    any config, transport, shape, key-id or signature-verification problem."""
    import requests
    env = os.environ
    viol = signer_config_violations(env)
    if viol:
        raise RuntimeError("external signer configuration invalid: " + viol[0])
    url = env["RELEASE_SIGNER_URL"].strip().rstrip("/")
    kid = key_id(env, purpose)
    try:
        r = requests.post(f"{url}/sign", json={"key_id": kid, "data_hex": data.hex(), "purpose": purpose},
                          headers={"Authorization": f"Bearer {_token_for(purpose, env)}"},
                          timeout=_timeout(env), verify=_tls_verify(env))
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
    if not verify_hex(data, sig, _pinned_pub(env, purpose), purpose=purpose):
        gen = "" if body.get("purpose") else " [the /sign response carries no `purpose` field: this URL answers with PRE-N100-11 code]"
        raise RuntimeError("external signer signature failed local verification against the pinned public key"
                           + _verification_failure_diagnosis(url, data, sig, _pinned_pub(env, purpose), env) + gen)
    return sig


def _verification_failure_diagnosis(url: str, data: bytes, sig: str, pinned: str, env) -> str:
    """Tell the operator WHICH of the two real-world causes it is: a signer still running pre-N100-11
    code (signs the raw bytes, ignores `purpose`) or a pin that is not the key the signer serves."""
    import requests
    if verify_hex(data, sig, pinned, purpose=None):
        return (" — the signer signed the RAW payload without the domain prefix: it runs pre-N100-11 code. "
                "Redeploy the signer CODE (cd deploy/signer && flyctl deploy -a <app>; never init_fly_signer.sh).")
    try:
        body = requests.get(f"{url}/public-key", timeout=_timeout(env), verify=_tls_verify(env)).json()
        served = (body.get("public_key_b64") or "").strip()
    except Exception:  # noqa: BLE001
        return " — signer /public-key unreachable; compare it with the pinned RELEASE_PUBLIC_KEY_B64 manually."
    if served and served != pinned:
        return (f" — the signer serves public key {served} but the pinned key is {pinned}: "
                "set RELEASE_PUBLIC_KEY_B64 (GitHub secret + server backend/.env) to the served key.")
    return " — key pin matches the served key; the signer did not sign the submitted bytes (check its logs)."


def signer_health(env=None) -> dict:
    """Non-signing connectivity/identity check: GET {url}/health must answer
    {ok:true, key_id, public_key_b64} matching the pinned identity of the key THIS host talks to
    (the bundle key when the host holds the bundle token, else the CI release key)."""
    import requests
    env = env if env is not None else os.environ
    purpose = "acceptance-bundle" if (env.get("RELEASE_SIGNER_BUNDLE_TOKEN") or "").strip() else "ea-release"
    out = {"mode": _mode(env), "ok": False, "key_id": key_id(env, purpose)}
    viol = signer_config_violations(env)
    if viol:
        out["error"] = viol[0]
        return out
    if _mode(env) == "deferred":
        out.update(ok=False, deferred=True, error=DEFERRED_MESSAGE)
        return out
    if _mode(env) != "external":
        out.update(ok=True, note="local signer — no remote identity to check")
        return out
    try:
        r = requests.get(f"{env['RELEASE_SIGNER_URL'].strip().rstrip('/')}/health",
                         headers={"Authorization": f"Bearer {_token_for(purpose, env)}"},
                         timeout=_timeout(env), verify=_tls_verify(env))
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        out["error"] = f"signer unreachable/malformed: {type(e).__name__}"
        return out
    out["remote_key_id"] = body.get("key_id")
    out["identity_matches"] = (body.get("key_id") == key_id(env, purpose)
                               and (body.get("public_key_b64") or "").strip() == _pinned_pub(env, purpose))
    out["ok"] = bool(body.get("ok")) and out["identity_matches"]
    if not out["ok"]:
        out["error"] = "signer identity mismatch" if not out["identity_matches"] else "signer reports not ok"
    return out


def public_key_b64(purpose: str = "ea-release") -> str:
    pinned = _pinned_pub(os.environ, purpose)
    if pinned:
        return pinned
    if _mode(os.environ) == "deferred":
        raise SignerDeferred(DEFERRED_MESSAGE)
    pub = _private_key().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(pub).decode()


def revoked_key_ids() -> set:
    return {k.strip() for k in (os.environ.get("RELEASE_REVOKED_KEY_IDS") or "").split(",") if k.strip()}


def key_id_accepted(kid, purpose: str = "ea-release") -> bool:
    """A14-7 — a signature only counts under the CURRENT, un-revoked key id for its purpose."""
    return bool(kid) and kid == key_id(purpose=purpose) and kid not in revoked_key_ids()


def verify_hex(data: bytes, signature_hex: str,
               pub_b64: str | None = None, *, purpose) -> bool:
    """`purpose` is REQUIRED (N101-5): None = raw/legacy externally produced signatures only."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(pub_b64 or public_key_b64(purpose or "ea-release")))
        pub.verify(bytes.fromhex(signature_hex), domain_bytes(purpose, data))
        return True
    except Exception:  # noqa: BLE001
        return False
