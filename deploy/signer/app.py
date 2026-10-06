"""STOIC external release signer — an ISOLATED signing microservice.

Runs on separate infrastructure from the API so the Ed25519 private key
never touches the trading backend (RELEASE_SIGNER=external).

Contract (matches backend/release_signing.py::_external_sign):
  POST /sign   Authorization: Bearer <SIGNER_TOKEN>
               {"key_id": "stoic-release-ed25519-v1", "data_hex": "..."}
               → {"signature_hex": "..."}
  GET  /public-key → {"key_id": ..., "public_key_b64": ...}
  GET  /health     Authorization: Bearer <SIGNER_TOKEN>
                   → {"ok": true, "key_id": ..., "public_key_b64": ...}
                   (identity check used by release_signing.signer_health)
  GET  /healthz    → {"status": "ok"}   (unauthenticated liveness for the platform)

Env:
  SIGNER_TOKEN               release token — CI only (ea-release, model-manifest, policy-migration)
  SIGNER_TOKEN_BUNDLE        runtime token — the trading API (acceptance-bundle, artifact-manifest, …).
                             Missing ⇒ RELEASE-ONLY mode: runtime purposes are refused (403). There is
                             no "one token signs everything" fallback (N101-5).
  SIGNER_KEY_ID              key id this signer answers for (default stoic-release-ed25519-v1; the
                             self-hosted runtime sidecar uses stoic-bundle-ed25519-v1)
  ED25519_SIGNING_KEY_B64    base64 raw 32-byte Ed25519 private key
"""
import base64
import hmac
import logging
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

KEY_ID = (os.environ.get("SIGNER_KEY_ID") or "stoic-release-ed25519-v1").strip()
_log = logging.getLogger("stoic.signer")

def _secret(name: str) -> str:
    """Env value, or the contents of the file named by <NAME>_FILE (Docker secrets)."""
    path = os.environ.get(f"{name}_FILE")
    if path:
        with open(path) as fh:
            return fh.read().strip()
    return os.environ[name]


_token = _secret("SIGNER_TOKEN")                       # release token — CI only: ea-release, model-manifest
try:
    _bundle_token = _secret("SIGNER_TOKEN_BUNDLE") or None   # API token — runtime purposes
except (KeyError, FileNotFoundError):
    _bundle_token = None
if not _bundle_token:
    _log.warning("SIGNER_TOKEN_BUNDLE not configured — RELEASE-ONLY mode: runtime purposes are refused (N101-5)")

# N100-11 — domain separation. Mirrors backend/release_signing.PURPOSES byte-for-byte.
PURPOSES = {
    "ea-release":        b"stoic:ea-release:v1\0",
    "model-manifest":    b"stoic:model-manifest:v1\0",
    "policy-migration":  b"stoic:policy-migration:v1\0",
    "acceptance-bundle": b"stoic:acceptance-bundle:v1\0",
    "artifact-manifest": b"stoic:artifact-manifest:v1\0",
    "audit-anchor":      b"stoic:audit-anchor:v1\0",
    "differentiation":   b"stoic:differentiation:v1\0",
    "canary":            b"stoic:canary:v1\0",
}
RELEASE_PURPOSES = {"ea-release", "model-manifest", "policy-migration"}
_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(_secret("ED25519_SIGNING_KEY_B64")))

app = FastAPI(title="STOIC Release Signer", docs_url=None, redoc_url=None)


class SignRequest(BaseModel):
    key_id: str
    data_hex: str
    purpose: str


def _auth(authorization: str | None) -> str:
    """Returns which token authenticated: 'release' or 'bundle'."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    presented = authorization[7:]
    if hmac.compare_digest(presented, _token):
        return "release"
    if _bundle_token and hmac.compare_digest(presented, _bundle_token):
        return "bundle"
    raise HTTPException(status_code=403, detail="Invalid signer token")


def _authorize_purpose(role: str, purpose: str) -> bytes:
    if purpose not in PURPOSES:
        raise HTTPException(status_code=400, detail="unknown signing purpose")
    if purpose in RELEASE_PURPOSES and role != "release":
        # the trading API holds only the bundle token → it can NEVER obtain an EA-release signature
        raise HTTPException(status_code=403, detail="this token may not sign release artefacts")
    if purpose not in RELEASE_PURPOSES and role != "bundle":
        # N101-5 — no single-token fallback: the release token never signs runtime artefacts,
        # even when no bundle token is configured (release-only signer)
        raise HTTPException(status_code=403, detail="runtime purposes require the bundle token")
    return PURPOSES[purpose]


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def _public_key_b64() -> str:
    pub = _key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(pub).decode()


@app.get("/public-key")
def public_key():
    return {"key_id": KEY_ID, "public_key_b64": _public_key_b64()}


@app.get("/health")
def health(authorization: str | None = Header(None)):
    _auth(authorization)
    return {"ok": True, "key_id": KEY_ID, "public_key_b64": _public_key_b64(),
            "release_only": _bundle_token is None}


@app.post("/sign")
def sign(req: SignRequest, authorization: str | None = Header(None)):
    role = _auth(authorization)
    prefix = _authorize_purpose(role, req.purpose)
    if req.key_id != KEY_ID:
        raise HTTPException(status_code=400,
                            detail=f"unknown key_id (expected {KEY_ID})")
    try:
        data = bytes.fromhex(req.data_hex)
    except ValueError:
        raise HTTPException(status_code=400, detail="data_hex is not hex")
    if len(data) > 1_048_576:
        raise HTTPException(status_code=413, detail="payload too large")
    return {"signature_hex": _key.sign(prefix + data).hex(), "key_id": KEY_ID, "purpose": req.purpose}
