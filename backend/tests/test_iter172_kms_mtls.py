"""iter-172 — #1 external (KMS/HSM) release signing + #5 per-installation
mTLS client certificates for Host Agents."""
import os
import sys
import uuid

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv

load_dotenv(os.path.join(_BACKEND_DIR, ".env"))

API = os.environ.get("REACT_APP_BACKEND_URL", "").rstrip("/") + "/api"


def _run(coro):
    from conftest import run_async
    return run_async(coro)


# ────────────────────────── #1 KMS / external signer ──────────────────────


def test_local_sign_verify_roundtrip():
    import release_signing
    body = b"iter172-payload"
    sig = release_signing.sign_hex(body, purpose="ea-release")
    assert release_signing.verify_hex(body, sig, purpose="ea-release")
    assert not release_signing.verify_hex(body + b"x", sig, purpose="ea-release")


def test_external_mode_requires_config(monkeypatch):
    import pytest
    import release_signing
    monkeypatch.setenv("RELEASE_SIGNER", "external")
    monkeypatch.delenv("RELEASE_SIGNER_URL", raising=False)
    monkeypatch.delenv("RELEASE_SIGNER_TOKEN", raising=False)
    monkeypatch.delenv("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD", raising=False)
    monkeypatch.delenv("ED25519_SIGNING_KEY_B64", raising=False)
    with pytest.raises(RuntimeError, match="RELEASE_SIGNER_URL"):
        release_signing.sign_hex(b"x", purpose="ea-release")


def _external_env(monkeypatch, release_signing):
    """Round 9 P1-01: external mode needs the COMPLETE configuration; the pinned
    public key here is derived from the test-local private key so the fake
    signer's signatures verify."""
    import base64
    from cryptography.hazmat.primitives import serialization
    pub = release_signing._private_key().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    priv = os.environ["ED25519_SIGNING_KEY_B64"]
    monkeypatch.setenv("RELEASE_SIGNER", "external")
    monkeypatch.setenv("RELEASE_SIGNER_URL", "https://signer.internal")
    monkeypatch.setenv("RELEASE_SIGNER_ALLOWED_HOSTS", "signer.internal")
    monkeypatch.setenv("RELEASE_SIGNER_TOKEN", "tok")
    monkeypatch.setenv("RELEASE_SIGNER_KEY_ID", release_signing.KEY_ID)
    monkeypatch.setenv("RELEASE_PUBLIC_KEY_B64", base64.b64encode(pub).decode())
    monkeypatch.setenv("RELEASE_SIGNER_TIMEOUT", "5")
    monkeypatch.delenv("ED25519_SIGNING_KEY_B64", raising=False)
    monkeypatch.delenv("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD", raising=False)
    return priv


def test_external_signer_delegates_and_verifies(monkeypatch):
    import base64
    import requests
    import release_signing
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv_b64 = _external_env(monkeypatch, release_signing)
    signer_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(priv_b64))
    seen = {}

    class _Resp:
        status_code = 200

        def __init__(self, sig):
            self._sig = sig

        def raise_for_status(self):
            pass

        def json(self):
            return {"signature_hex": self._sig}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        data = bytes.fromhex(json["data_hex"])
        return _Resp(signer_key.sign(data).hex())

    monkeypatch.setattr(requests, "post", fake_post)
    body = b"iter172-external"
    sig = release_signing.sign_hex(body, purpose="ea-release")
    assert release_signing.verify_hex(body, sig, purpose="ea-release")
    assert seen["url"] == "https://signer.internal/sign"
    assert seen["auth"] == "Bearer tok"


def test_external_signer_bad_signature_rejected(monkeypatch):
    import pytest
    import requests
    import release_signing
    _external_env(monkeypatch, release_signing)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"signature_hex": "00" * 64}

    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError, match="failed local verification"):
        release_signing.sign_hex(b"forged", purpose="ea-release")


def test_local_signing_forbidden_in_production(monkeypatch):
    import pytest
    import release_signing
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("RELEASE_SIGNER", "local")
    monkeypatch.delenv("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD", raising=False)
    monkeypatch.delenv("RELEASE_SIGNER_DEFERRED", raising=False)
    with pytest.raises(RuntimeError, match="forbids RELEASE_SIGNER=local"):
        release_signing.sign_hex(b"x", purpose="ea-release")
    # v56: the escape hatch was REMOVED — the override no longer works
    monkeypatch.setenv("RELEASE_SIGNER_ALLOW_LOCAL_IN_PROD", "true")
    with pytest.raises(RuntimeError, match="forbids RELEASE_SIGNER=local"):
        release_signing.sign_hex(b"x", purpose="ea-release")


def test_signer_status_exposed_on_release_key():
    import requests
    import release_signing
    st = release_signing.signer_status()
    assert st["mode"] == "local" and st["key_id"] == release_signing.KEY_ID
    r = requests.get(f"{API}/release-key", timeout=15)
    assert r.status_code == 200, r.text
    assert r.json()["signer"]["mode"] == "local"


# ────────────────────────── #5 per-installation mTLS ──────────────────────


def _mongo():
    from tests.helpers import mongo_db
    return mongo_db()


def _seed_agent(db, uid=None):
    from vps_agent import hash_agent_token
    uid = uid or f"iter172-{uuid.uuid4().hex[:8]}"
    agent_id = f"agt_{uuid.uuid4().hex[:12]}"
    token = f"agt_tok_{uuid.uuid4().hex}"
    db.vps_agents.insert_one({
        "agent_id": agent_id, "agent_token_hash": hash_agent_token(token),
        "command_key": "k", "user_id": uid,
        "deployment_id": f"dep-{uid}", "revoked": False})
    return agent_id, token, uid


def _make_csr(cn: str) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name(
               [x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
           .sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def _fp(cert_pem: str) -> str:
    import hashlib
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    cert = x509.load_pem_x509_certificate(cert_pem.encode())
    return hashlib.sha256(
        cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def test_enroll_issues_cert_and_pins_fingerprint():
    import requests
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    db = _mongo()
    agent_id, token, uid = _seed_agent(db)
    try:
        r = requests.post(f"{API}/infra/agent/cert/enroll",
                          json={"agent_token": token,
                                "csr_pem": _make_csr(agent_id)}, timeout=20)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["agent_id"] == agent_id and body["ca_pem"]
        cert = x509.load_pem_x509_certificate(
            body["certificate_pem"].encode())
        cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        assert cn == agent_id, "cert must be bound to the agent identity"
        doc = db.vps_agents.find_one({"agent_id": agent_id})
        assert doc["mtls"]["fingerprint"] == body["fingerprint"]
        assert body["fingerprint"] == _fp(body["certificate_pem"])
        # bad token → 401; missing CSR → 400
        assert requests.post(f"{API}/infra/agent/cert/enroll",
                             json={"agent_token": "agt_tok_bogus",
                                   "csr_pem": "x"},
                             timeout=15).status_code == 401
    finally:
        db.vps_agents.delete_many({"user_id": uid})


def test_heartbeat_enforces_enrolled_cert():
    import requests
    db = _mongo()
    agent_id, token, uid = _seed_agent(db)
    hb = {"agent_token": token, "metrics": {"cpu_pct": 5}}
    try:
        # pre-enrollment: no cert required (opt-in rollout)
        assert requests.post(f"{API}/infra/agent/heartbeat", json=hb,
                             timeout=20).status_code == 200
        r = requests.post(f"{API}/infra/agent/cert/enroll",
                          json={"agent_token": token,
                                "csr_pem": _make_csr(agent_id)}, timeout=20)
        fp = r.json()["fingerprint"]
        # enrolled: missing / wrong fingerprint → 401
        assert requests.post(f"{API}/infra/agent/heartbeat", json=hb,
                             timeout=15).status_code == 401
        assert requests.post(
            f"{API}/infra/agent/heartbeat", json=hb,
            headers={"X-Client-Cert-Fingerprint": "ff" * 32},
            timeout=15).status_code == 401
        # correct fingerprint → 200 (case-insensitive)
        assert requests.post(
            f"{API}/infra/agent/heartbeat", json=hb,
            headers={"X-Client-Cert-Fingerprint": fp.upper()},
            timeout=15).status_code == 200
        # other agent-token endpoints share the same gate
        assert requests.post(
            f"{API}/infra/agent/commands/poll", json={"agent_token": token},
            timeout=15).status_code == 401
        assert requests.post(
            f"{API}/infra/agent/commands/poll", json={"agent_token": token},
            headers={"X-Client-Cert-Fingerprint": fp},
            timeout=15).status_code == 200
    finally:
        db.vps_agents.delete_many({"user_id": uid})


def test_reenrollment_requires_current_cert():
    import requests
    db = _mongo()
    agent_id, token, uid = _seed_agent(db)
    try:
        r1 = requests.post(f"{API}/infra/agent/cert/enroll",
                           json={"agent_token": token,
                                 "csr_pem": _make_csr(agent_id)}, timeout=20)
        fp1 = r1.json()["fingerprint"]
        # stolen bearer token alone cannot re-key the installation
        r2 = requests.post(f"{API}/infra/agent/cert/enroll",
                           json={"agent_token": token,
                                 "csr_pem": _make_csr(agent_id)}, timeout=20)
        assert r2.status_code == 401, r2.text
        # presenting the current cert allows rotation
        r3 = requests.post(f"{API}/infra/agent/cert/enroll",
                           json={"agent_token": token,
                                 "csr_pem": _make_csr(agent_id)},
                           headers={"X-Client-Cert-Fingerprint": fp1},
                           timeout=20)
        assert r3.status_code == 200, r3.text
        assert r3.json()["fingerprint"] != fp1
    finally:
        db.vps_agents.delete_many({"user_id": uid})


def test_owner_revoke_blocks_agent_until_reenroll():
    import requests
    from tests.helpers import mongo_db, register_and_login
    db = mongo_db()
    email = f"iter172-{uuid.uuid4().hex[:8]}@example.com"
    s = register_and_login(email)
    uid = str(db.users.find_one({"email": email})["_id"])
    agent_id, token, _ = _seed_agent(db, uid=uid)
    hb = {"agent_token": token, "metrics": {}}
    try:
        r = requests.post(f"{API}/infra/agent/cert/enroll",
                          json={"agent_token": token,
                                "csr_pem": _make_csr(agent_id)}, timeout=20)
        fp = r.json()["fingerprint"]
        # owner revokes the installation cert
        rv = s.post(f"{API}/infra/agents/{agent_id}/mtls/revoke",
                    json={"reason": "vps compromised"}, timeout=15)
        assert rv.status_code == 200, rv.text
        assert requests.post(
            f"{API}/infra/agent/heartbeat", json=hb,
            headers={"X-Client-Cert-Fingerprint": fp},
            timeout=15).status_code == 401
        # re-enrollment with the agent_token restores access
        r2 = requests.post(f"{API}/infra/agent/cert/enroll",
                           json={"agent_token": token,
                                 "csr_pem": _make_csr(agent_id)}, timeout=20)
        assert r2.status_code == 200, r2.text
        assert requests.post(
            f"{API}/infra/agent/heartbeat", json=hb,
            headers={"X-Client-Cert-Fingerprint": r2.json()["fingerprint"]},
            timeout=15).status_code == 200
        # a stranger cannot revoke someone else's agent
        email2 = f"iter172b-{uuid.uuid4().hex[:8]}@example.com"
        s2 = register_and_login(email2)
        assert s2.post(f"{API}/infra/agents/{agent_id}/mtls/revoke",
                       timeout=15).status_code == 404
    finally:
        db.vps_agents.delete_many({"user_id": uid})


def test_global_required_mode_blocks_unenrolled(monkeypatch):
    import pytest
    from agent_mtls import enforce_mtls
    from database import get_db
    monkeypatch.setenv("AGENT_MTLS_REQUIRED", "true")
    with pytest.raises(ValueError, match="mtls required"):
        _run(enforce_mtls(get_db(), {"agent_id": "agt_x"}, ""))
    monkeypatch.setenv("AGENT_MTLS_REQUIRED", "false")
    _run(enforce_mtls(get_db(), {"agent_id": "agt_x"}, ""))  # no raise


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.http
