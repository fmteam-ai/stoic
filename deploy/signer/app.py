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

Env (both REQUIRED — the service refuses to start without them):
  SIGNER_TOKEN               bearer token the API must present
  ED25519_SIGNING_KEY_B64    base64 raw 32-byte Ed25519 private key
"""
import base64
import hmac
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

KEY_ID = "stoic-release-ed25519-v1"

_token = os.environ["SIGNER_TOKEN"]
_key = Ed25519PrivateKey.from_private_bytes(
    base64.b64decode(os.environ["ED25519_SIGNING_KEY_B64"]))

app = FastAPI(title="STOIC Release Signer", docs_url=None, redoc_url=None)


class SignRequest(BaseModel):
    key_id: str
    data_hex: str


def _auth(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    if not hmac.compare_digest(authorization[7:], _token):
        raise HTTPException(status_code=403, detail="Invalid signer token")


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
    return {"ok": True, "key_id": KEY_ID, "public_key_b64": _public_key_b64()}


@app.post("/sign")
def sign(req: SignRequest, authorization: str | None = Header(None)):
    _auth(authorization)
    if req.key_id != KEY_ID:
        raise HTTPException(status_code=400,
                            detail=f"unknown key_id (expected {KEY_ID})")
    try:
        data = bytes.fromhex(req.data_hex)
    except ValueError:
        raise HTTPException(status_code=400, detail="data_hex is not hex")
    if len(data) > 1_048_576:
        raise HTTPException(status_code=413, detail="payload too large")
    return {"signature_hex": _key.sign(data).hex(), "key_id": KEY_ID}
