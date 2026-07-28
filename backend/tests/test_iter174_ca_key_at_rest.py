"""iter-174 · SEC-004 — the host-agent issuing CA private key is ENCRYPTED
at rest in platform_state (a stolen DB read alone can't yield the signing
key), with an idempotent migration from any legacy plaintext key_pem."""
import os
import sys

_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_DIR)
from dotenv import load_dotenv

load_dotenv(os.path.join(_BACKEND_DIR, ".env"))


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db()


def _reset_ca(db):
    _run(db.platform_state.delete_one({"_id": "agent_ca"}))


def test_ca_key_encrypted_at_rest():
    import agent_mtls
    db = _db()
    _reset_ca(db)
    try:
        ca_key, ca_cert = _run(agent_mtls._get_ca(db))
        st = _run(db.platform_state.find_one({"_id": "agent_ca"}))
        assert "key_pem" not in st, "plaintext CA key stored at rest!"
        assert st.get("key_pem_enc"), "CA key must be stored encrypted"
        assert "PRIVATE KEY" not in str(st["key_pem_enc"])
        # the encrypted blob must actually decrypt back to a usable key
        import secrets_vault
        pem = secrets_vault.decrypt(st["key_pem_enc"],
                                    associated_data=b"agent_ca")
        assert "PRIVATE KEY" in pem
        # AAD is bound — wrong context fails
        try:
            secrets_vault.decrypt(st["key_pem_enc"], associated_data=b"wrong")
            assert False, "AAD mismatch must fail"
        except Exception:
            pass
    finally:
        _reset_ca(db)


def test_legacy_plaintext_ca_migrated():
    import datetime as dt

    import agent_mtls
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    db = _db()
    _reset_ca(db)
    # seed a legacy plaintext CA exactly like the old code did
    key = ec.generate_private_key(ec.SECP256R1())
    subj = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "legacy CA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subj).issuer_name(subj)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    _run(db.platform_state.insert_one({
        "_id": "agent_ca", "key_pem": key_pem,
        "cert_pem": cert.public_bytes(serialization.Encoding.PEM).decode()}))
    try:
        # first load migrates plaintext → encrypted and drops the plaintext
        _run(agent_mtls._get_ca(db))
        st = _run(db.platform_state.find_one({"_id": "agent_ca"}))
        assert "key_pem" not in st, "legacy plaintext not dropped"
        assert st.get("key_pem_enc")
        # same CA identity preserved (not regenerated)
        _, ca_cert2 = _run(agent_mtls._get_ca(db))
        assert ca_cert2.serial_number == cert.serial_number
    finally:
        _reset_ca(db)


def test_enrollment_still_works_after_encryption():
    import uuid

    import agent_mtls
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    db = _db()
    _reset_ca(db)
    agent_id = f"agt_{uuid.uuid4().hex[:12]}"
    k = ec.generate_private_key(ec.SECP256R1())
    csr = (x509.CertificateSigningRequestBuilder().subject_name(
        x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, agent_id)]))
        .sign(k, hashes.SHA256()))
    try:
        out = _run(agent_mtls.issue_from_csr(
            db, {"agent_id": agent_id},
            csr.public_bytes(serialization.Encoding.PEM).decode()))
        assert out["agent_id"] == agent_id and out["fingerprint"]
        # cert chains to the (encrypted-at-rest) CA
        cert = x509.load_pem_x509_certificate(out["certificate_pem"].encode())
        ca = x509.load_pem_x509_certificate(out["ca_pem"].encode())
        ca.public_key().verify(
            cert.signature, cert.tbs_certificate_bytes,
            __import__("cryptography.hazmat.primitives.asymmetric.ec",
                       fromlist=["ECDSA"]).ECDSA(cert.signature_hash_algorithm))
    finally:
        _run(db.vps_agents.delete_many({"agent_id": agent_id}))
        _reset_ca(db)
