"""Standalone release-signing service (reference implementation for #1).

Runs OUTSIDE the API, in an isolated network segment (or as a thin proxy in
front of AWS KMS / GCP Cloud KMS / an HSM). The API is configured with
RELEASE_SIGNER=external + RELEASE_SIGNER_URL/RELEASE_SIGNER_TOKEN and never
holds the Ed25519 private key.

Contract (matched by backend/release_signing.py::_external_sign):
    POST /sign   Authorization: Bearer <SIGNER_BEARER_TOKEN>
    body {"key_id": "...", "data_hex": "<hex>"}
    → 200 {"signature_hex": "<hex>", "key_id": "..."}

Env:
    ED25519_SIGNING_KEY_B64  raw 32-byte key, base64 (or swap _sign() for a
                             KMS SDK call)
    SIGNER_BEARER_TOKEN      shared bearer token the API must present
    SIGNER_PORT              default 8944 (bind 127.0.0.1 / private subnet)

Run:  python scripts/release_signer_service.py
"""
import base64
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEY_ID = "stoic-release-ed25519-v1"
MAX_BODY = 1 << 20


def _key() -> Ed25519PrivateKey:
    b64 = os.environ["ED25519_SIGNING_KEY_B64"]
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(b64))


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: dict):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        if self.path != "/sign":
            return self._reply(404, {"error": "not found"})
        expected = f"Bearer {os.environ['SIGNER_BEARER_TOKEN']}"
        got = self.headers.get("Authorization", "")
        if not hmac.compare_digest(got, expected):
            return self._reply(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            return self._reply(413, {"error": "payload too large"})
        try:
            body = json.loads(self.rfile.read(length))
            data = bytes.fromhex(body["data_hex"])
        except (ValueError, KeyError):
            return self._reply(400, {"error": "bad request"})
        if body.get("key_id") != KEY_ID:
            return self._reply(400, {"error": "unknown key_id"})
        sig = _key().sign(data).hex()
        self._reply(200, {"signature_hex": sig, "key_id": KEY_ID})

    def log_message(self, fmt, *args):  # keep signing requests out of stdout
        pass


if __name__ == "__main__":
    port = int(os.environ.get("SIGNER_PORT", "8944"))
    _key()  # fail fast if the key is missing
    print(f"release-signer listening on 127.0.0.1:{port}")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
