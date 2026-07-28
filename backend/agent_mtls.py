"""iter-171 (#5) — per-installation mTLS client certificates.

After an agent enrolls (proves possession of its agent_token), it submits a
CSR and receives a short-lived client certificate bound to its agent_id
(CN=agent_id). The cert fingerprint is pinned on the agent record; the ingress
/ API can then require that every subsequent agent request presents a client
cert whose fingerprint matches the enrolled one (defence beyond the bearer
token — a stolen token alone no longer impersonates a legitimate install).

The CA private key is loaded from AGENT_CA_KEY_PEM / AGENT_CA_CERT_PEM in
production (keep it in KMS/secret store). For preview/dev a CA is generated
once and persisted in platform_state so tests are deterministic.
"""
import datetime as _dt
import hashlib
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CERT_DAYS = int(os.environ.get("AGENT_CERT_DAYS", "90"))


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


async def _get_ca(db):
    """Load the issuing CA. Preference order:
      1. AGENT_CA_KEY_PEM / AGENT_CA_CERT_PEM from env (KMS/secret store) — prod.
      2. Persisted CA in platform_state, with the private key ENCRYPTED at
         rest via secrets_vault (SEC-004). A stolen DB read alone no longer
         yields the CA signing key.
    Legacy plaintext key_pem is read once, re-encrypted, and the plaintext
    dropped (idempotent migration)."""
    import secrets_vault
    key_pem = os.environ.get("AGENT_CA_KEY_PEM")
    cert_pem = os.environ.get("AGENT_CA_CERT_PEM")
    if key_pem and cert_pem:
        return (serialization.load_pem_private_key(key_pem.encode(), None),
                x509.load_pem_x509_certificate(cert_pem.encode()))
    st = await db.platform_state.find_one({"_id": "agent_ca"})
    if st and st.get("cert_pem"):
        if st.get("key_pem_enc"):
            key_pem = secrets_vault.decrypt(st["key_pem_enc"],
                                            associated_data=b"agent_ca")
        elif st.get("key_pem"):  # legacy plaintext → migrate in place
            key_pem = st["key_pem"]
            await db.platform_state.update_one(
                {"_id": "agent_ca"},
                {"$set": {"key_pem_enc": secrets_vault.encrypt(
                    key_pem, associated_data=b"agent_ca")},
                 "$unset": {"key_pem": ""}})
        else:
            key_pem = None
        if key_pem:
            return (serialization.load_pem_private_key(key_pem.encode(), None),
                    x509.load_pem_x509_certificate(st["cert_pem"].encode()))
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "STOIC Host-Agent Dev CA"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "STOIC AI")])
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - _dt.timedelta(minutes=1))
            .not_valid_after(_now() + _dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .sign(key, hashes.SHA256()))
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    await db.platform_state.update_one(
        {"_id": "agent_ca"},
        {"$setOnInsert": {
            "key_pem_enc": secrets_vault.encrypt(
                key_pem, associated_data=b"agent_ca"),
            "cert_pem": cert_pem, "created_at": _now().isoformat()}},
        upsert=True)
    st = await db.platform_state.find_one({"_id": "agent_ca"})
    stored = (secrets_vault.decrypt(st["key_pem_enc"],
                                    associated_data=b"agent_ca")
              if st.get("key_pem_enc") else st.get("key_pem"))
    return (serialization.load_pem_private_key(stored.encode(), None),
            x509.load_pem_x509_certificate(st["cert_pem"].encode()))


def _fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(
        cert.public_bytes(serialization.Encoding.DER)).hexdigest()


async def issue_from_csr(db, agent: dict, csr_pem: str) -> dict:
    """Issue a per-installation client cert from the agent's CSR."""
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    if not csr.is_signature_valid:
        raise ValueError("CSR signature invalid")
    ca_key, ca_cert = await _get_ca(db)
    agent_id = agent["agent_id"]
    not_after = _now() + _dt.timedelta(days=CERT_DAYS)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, agent_id)]))
            .issuer_name(ca_cert.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - _dt.timedelta(minutes=1))
            .not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.ExtendedKeyUsage(
                [x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]), False)
            .sign(ca_key, hashes.SHA256()))
    fp = _fingerprint(cert)
    await db.vps_agents.update_one(
        {"agent_id": agent_id},
        {"$set": {"mtls": {"fingerprint": fp,
                           "serial": str(cert.serial_number),
                           "issued_at": _now().isoformat(),
                           "not_after": not_after.isoformat(),
                           "revoked": False}}})
    return {"agent_id": agent_id,
            "certificate_pem": cert.public_bytes(
                serialization.Encoding.PEM).decode(),
            "ca_pem": ca_cert.public_bytes(
                serialization.Encoding.PEM).decode(),
            "fingerprint": fp, "not_after": not_after.isoformat()}


async def enforce_mtls(db, agent: dict, fingerprint: str) -> None:
    """Gate for agent API calls (SEC-004 — honest posture).

    SECURITY MODEL: today the ingress does NOT terminate client-cert mTLS, so
    the fingerprint arrives as a plain X-Client-Cert-Fingerprint header. This
    is therefore a DEFENCE-IN-DEPTH layer, not cryptographic proof of
    possession: the pinned fingerprint acts as a SECOND per-installation
    secret on top of the bearer agent_token (a stolen token alone no longer
    suffices — the attacker also needs the enrolled cert's fingerprint).
    For true proof-of-possession, terminate client-cert mTLS at the edge and
    have the ingress inject a TRUSTED fingerprint header (strip any
    client-supplied one), then set AGENT_MTLS_REQUIRED=true.

    Behaviour: once an agent has an enrolled cert (or AGENT_MTLS_REQUIRED=true
    globally), every request must present the matching fingerprint. Raises
    ValueError on rejection."""
    required = os.environ.get("AGENT_MTLS_REQUIRED", "").strip().lower() in (
        "1", "true", "yes")
    if not (agent or {}).get("mtls"):
        if required:
            raise ValueError("mtls required: no client certificate enrolled "
                             "for this agent")
        return
    res = await verify_agent_cert(db, agent["agent_id"], fingerprint)
    if not res["ok"]:
        raise ValueError(f"mtls rejected: {res['reason']}")


async def revoke_agent_cert(db, agent_id: str, reason: str = "") -> bool:
    r = await db.vps_agents.update_one(
        {"agent_id": agent_id, "mtls": {"$exists": True}},
        {"$set": {"mtls.revoked": True,
                  "mtls.revoked_at": _now().isoformat(),
                  "mtls.revoked_reason": reason[:200]}})
    return r.matched_count == 1


async def verify_agent_cert(db, agent_id: str, fingerprint: str) -> dict:
    """Check a presented client-cert fingerprint against the enrolled cert."""
    agent = await db.vps_agents.find_one({"agent_id": agent_id})
    m = (agent or {}).get("mtls") or {}
    if not m:
        return {"ok": False, "reason": "no_cert_enrolled"}
    if m.get("revoked"):
        return {"ok": False, "reason": "revoked"}
    if not fingerprint or fingerprint.lower() != str(m["fingerprint"]).lower():
        return {"ok": False, "reason": "fingerprint_mismatch"}
    try:
        if _now() > _dt.datetime.fromisoformat(m["not_after"]):
            return {"ok": False, "reason": "expired"}
    except (KeyError, ValueError):
        pass
    return {"ok": True, "agent_id": agent_id}
