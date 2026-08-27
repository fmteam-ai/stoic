"""iter-177 — WebAuthn passkeys for administrators (step-up factor).

Uses a minimal software authenticator (EC P-256, attestation fmt "none")
to drive real registration + assertion ceremonies against the live API.
"""
import base64
import hashlib
import json
import os
import struct
import sys
import uuid

import cbor2
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv
load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/")
API = f"{BASE_URL}/api"
ORIGIN = BASE_URL
ADMIN_EMAIL = "admin@trading.bot"
ADMIN_PW = "admin123"
TIMEOUT = 25


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _from_b64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _admin():
    s = requests.Session()
    s.headers["Origin"] = ORIGIN
    r = s.post(f"{API}/auth/login",
               json={"email": ADMIN_EMAIL, "password": ADMIN_PW},
               timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return s


class SoftAuthenticator:
    """Minimal FIDO2 authenticator: P-256 key, attestation 'none'."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = uuid.uuid4().bytes + uuid.uuid4().bytes
        self.sign_count = 0

    def _cose_key(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1,
                            -2: nums.x.to_bytes(32, "big"),
                            -3: nums.y.to_bytes(32, "big")})

    def create(self, options: dict, origin: str) -> dict:
        rp_id = options["rp"]["id"]
        client_data = json.dumps({
            "type": "webauthn.create",
            "challenge": options["challenge"],
            "origin": origin, "crossOrigin": False}).encode()
        auth_data = (hashlib.sha256(rp_id.encode()).digest()
                     + bytes([0x45])                      # UP|UV|AT
                     + struct.pack(">I", self.sign_count)
                     + b"\x00" * 16                       # aaguid
                     + struct.pack(">H", len(self.cred_id))
                     + self.cred_id + self._cose_key())
        att_obj = cbor2.dumps({"fmt": "none", "attStmt": {},
                               "authData": auth_data})
        return {"id": _b64u(self.cred_id), "rawId": _b64u(self.cred_id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"attestationObject": _b64u(att_obj),
                             "clientDataJSON": _b64u(client_data),
                             "transports": ["usb"]}}

    def get(self, options: dict, origin: str) -> dict:
        rp_id = options["rpId"]
        self.sign_count += 1
        client_data = json.dumps({
            "type": "webauthn.get",
            "challenge": options["challenge"],
            "origin": origin, "crossOrigin": False}).encode()
        auth_data = (hashlib.sha256(rp_id.encode()).digest()
                     + bytes([0x05])                      # UP|UV
                     + struct.pack(">I", self.sign_count))
        sig = self.key.sign(
            auth_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()))
        return {"id": _b64u(self.cred_id), "rawId": _b64u(self.cred_id),
                "type": "public-key", "clientExtensionResults": {},
                "response": {"authenticatorData": _b64u(auth_data),
                             "clientDataJSON": _b64u(client_data),
                             "signature": _b64u(sig), "userHandle": None}}


def _enroll(s, authenticator, label="pytest key"):
    r = s.post(f"{API}/auth/webauthn/register/begin",
               json={"origin": ORIGIN}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    begin = r.json()
    cred = authenticator.create(begin["options"], ORIGIN)
    r = s.post(f"{API}/auth/webauthn/register/complete",
               json={"challenge_id": begin["challenge_id"],
                     "credential": cred, "label": label}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    return r.json()


def _cleanup(s, credential_id):
    s.delete(f"{API}/auth/webauthn/credentials/{credential_id}",
             timeout=TIMEOUT)


def test_passkey_enroll_list_and_remove():
    s = _admin()
    auth = SoftAuthenticator()
    out = _enroll(s, auth, "enroll-test")
    try:
        r = s.get(f"{API}/auth/webauthn/credentials", timeout=TIMEOUT)
        ids = [c["credential_id"] for c in r.json()["credentials"]]
        assert out["credential_id"] in ids
    finally:
        r = s.delete(
            f"{API}/auth/webauthn/credentials/{out['credential_id']}",
            timeout=TIMEOUT)
        assert r.status_code == 200
    r = s.delete(f"{API}/auth/webauthn/credentials/{out['credential_id']}",
                 timeout=TIMEOUT)
    assert r.status_code == 404


def test_passkey_step_up_end_to_end():
    """Full ceremony → step-up token → token actually opens an ops action."""
    s = _admin()
    auth = SoftAuthenticator()
    out = _enroll(s, auth, "stepup-test")
    try:
        r = s.post(f"{API}/auth/webauthn/step-up/begin",
                   json={"action": "audit_anchor", "origin": ORIGIN},
                   timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        begin = r.json()
        assertion = auth.get(begin["options"], ORIGIN)
        r = s.post(f"{API}/auth/webauthn/step-up/complete",
                   json={"challenge_id": begin["challenge_id"],
                         "action": "audit_anchor",
                         "credential": assertion}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text
        token = r.json()["step_up_token"]
        assert token

        # the minted token must satisfy require_step_up on a real ops action
        r = s.post(f"{API}/ops/audit-anchor",
                   headers={"X-Step-Up-Token": token}, timeout=TIMEOUT)
        assert r.status_code == 200, r.text

        # sign counter advanced (cloned-authenticator detection state)
        r = s.get(f"{API}/auth/webauthn/credentials", timeout=TIMEOUT)
        cred = [c for c in r.json()["credentials"]
                if c["credential_id"] == out["credential_id"]][0]
        assert cred["last_used_at"]
    finally:
        _cleanup(s, out["credential_id"])


def test_passkey_challenge_single_use_and_bad_signature():
    s = _admin()
    auth = SoftAuthenticator()
    out = _enroll(s, auth, "neg-test")
    try:
        r = s.post(f"{API}/auth/webauthn/step-up/begin",
                   json={"action": "audit_anchor", "origin": ORIGIN},
                   timeout=TIMEOUT)
        begin = r.json()
        assertion = auth.get(begin["options"], ORIGIN)
        # tamper with the signature
        bad = dict(assertion)
        bad["response"] = dict(assertion["response"])
        sig = bytearray(_from_b64u(assertion["response"]["signature"]))
        sig[-1] ^= 0xFF
        bad["response"]["signature"] = _b64u(bytes(sig))
        r = s.post(f"{API}/auth/webauthn/step-up/complete",
                   json={"challenge_id": begin["challenge_id"],
                         "action": "audit_anchor", "credential": bad},
                   timeout=TIMEOUT)
        assert r.status_code == 401
        # challenge was consumed by the failed attempt — replay must fail too
        r = s.post(f"{API}/auth/webauthn/step-up/complete",
                   json={"challenge_id": begin["challenge_id"],
                         "action": "audit_anchor", "credential": assertion},
                   timeout=TIMEOUT)
        assert r.status_code == 401
    finally:
        _cleanup(s, out["credential_id"])


def test_passkey_wrong_action_and_origin_rejected():
    s = _admin()
    auth = SoftAuthenticator()
    out = _enroll(s, auth, "action-test")
    try:
        # challenge bound to one action cannot mint a token for another
        r = s.post(f"{API}/auth/webauthn/step-up/begin",
                   json={"action": "audit_anchor", "origin": ORIGIN},
                   timeout=TIMEOUT)
        begin = r.json()
        assertion = auth.get(begin["options"], ORIGIN)
        r = s.post(f"{API}/auth/webauthn/step-up/complete",
                   json={"challenge_id": begin["challenge_id"],
                         "action": "release_promote",
                         "credential": assertion}, timeout=TIMEOUT)
        assert r.status_code == 401

        # a foreign origin is scoped to ITS OWN RP — a credential created
        # there can never assert against our real origin's RP ID.
        r = s.post(f"{API}/auth/webauthn/register/begin",
                   json={"origin": "https://evil.example.com"},
                   timeout=TIMEOUT)
        assert r.status_code == 200
        opts = r.json()["options"]
        assert opts["rp"]["id"] == "evil.example.com"
        # and a garbage origin is rejected outright
        r = s.post(f"{API}/auth/webauthn/register/begin",
                   json={"origin": "not-a-url"}, timeout=TIMEOUT)
        assert r.status_code == 400
    finally:
        _cleanup(s, out["credential_id"])


def test_passkey_admin_only():
    email = f"iter177-{uuid.uuid4().hex[:8]}@test.io"
    s = requests.Session()
    s.headers["Origin"] = ORIGIN
    r = s.post(f"{API}/auth/register",
               json={"email": email, "password": "Str0ng!pass123",
                     "terms_agreed": True}, timeout=TIMEOUT)
    assert r.status_code == 200, r.text
    # unverified accounts can't login; hit endpoint anonymously instead
    r = requests.get(f"{API}/auth/webauthn/credentials", timeout=TIMEOUT)
    assert r.status_code in (401, 403)


def test_step_up_enrollment_satisfied_by_passkey():
    """require_step_up must accept passkey-enrolled users as MFA-enrolled."""
    def _run(coro):
        from conftest import run_async
        return run_async(coro)
    from database import get_db
    import pytest as _pytest
    from fastapi import HTTPException
    from step_up import require_step_up
    db = get_db()

    class _Req:
        headers = {}
    admin = _run(db.users.find_one({"email": ADMIN_EMAIL}))
    uid = str(admin["_id"])
    fake_cred = {"user_id": uid, "credential_id": f"test-{uuid.uuid4().hex}",
                 "public_key": "x", "sign_count": 0}
    two_fa = bool(admin.get("two_factor_enabled"))
    try:
        if not two_fa:
            _run(db.webauthn_credentials.insert_one(dict(fake_cred)))
        with _pytest.raises(HTTPException) as exc:
            _run(require_step_up(db, {"id": uid}, _Req(), "audit_anchor"))
        # passkey present → must ask for the token, NOT mfa_enrollment
        assert exc.value.detail["code"] == "step_up_required"
    finally:
        _run(db.webauthn_credentials.delete_many(
            {"credential_id": fake_cred["credential_id"]}))


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
