"""r26 P1-02 / AT-P1-02 — cryptographic EA installation proof.

Only a fresh signature from the ENROLLED installer device key over the exact
measured EX5 + terminal binding is admitted; bridge credentials alone can never
create `installer_attested`."""
import asyncio
import base64
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("MONGO_URL"), reason="needs Mongo")]

import device_attestation as da  # noqa: E402

PINNED = "d" * 64


def _run(coro):
    from conftest import run_async
    return run_async(coro)


def _db():
    from database import get_db
    return get_db().client[f"stoic_r26_att_{uuid.uuid4().hex[:8]}"]


def _b64_be(n: int) -> str:
    return base64.b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).decode()


def dotnet_xml(pub) -> str:
    """Exactly what RSACng.ToXmlString($false) emits: <RSAKeyValue><Modulus/><Exponent/></RSAKeyValue>."""
    nums = pub.public_numbers()
    return f"<RSAKeyValue><Modulus>{_b64_be(nums.n)}</Modulus><Exponent>{_b64_be(nums.e)}</Exponent></RSAKeyValue>"


def sign_rsa(priv, payload: dict) -> str:
    return base64.b64encode(priv.sign(da.canonical(payload),
                                      padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                  salt_length=padding.PSS.DIGEST_LENGTH),
                                      hashes.SHA256())).decode()


def proof(inst, nonce, **over):
    p = {"installation_id": inst, "nonce": nonce, "terminal_identity": "A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6",
         "ex5_sha256": PINNED, "capabilities": ["ex5_measured", "installer_v1_1"], "ts": int(time.time())}
    p.update(over)
    return p


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("EA_RELEASE_SHA256", PINNED)
    monkeypatch.setattr(da, "CHALLENGE_INST_LIMIT_PER_MIN", 1000)     # abuse limits have their own test
    db = _db()
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    inst = f"inst_{uuid.uuid4().hex[:12]}"
    acc_id = ObjectId()
    dk = da.device_key_record("RSA-PSS-SHA256", dotnet_xml(priv.public_key()))
    _run(db.installations.insert_one({"installation_id": inst, "account_id": str(acc_id), "user_id": "u1",
                                      "revoked": False, "device_key": dk}))
    _run(da.ensure_indexes(db))
    try:
        yield {"db": db, "priv": priv, "inst": inst, "acc_id": acc_id, "dk": dk}
    finally:
        _run(db.client.drop_database(db.name))


def _challenge(w):
    return _run(da.issue_challenge(w["db"], w["inst"]))["nonce"]


def _attest(w, body):
    return _run(da.verify_attestation(w["db"], body))


def test_canonical_golden_vector_matches_powershell_builder():
    p = {"ts": 1730000000, "nonce": "n-1_x", "capabilities": ["ex5_measured", "dpapi_key"], "installation_id": "inst_ab",
         "terminal_identity": "TERM 1", "ex5_sha256": "a" * 64, "signature": "must-not-be-signed"}
    assert da.canonical(p) == (b'{"capabilities":["ex5_measured","dpapi_key"],"ex5_sha256":"' + b"a" * 64 +
                               b'","installation_id":"inst_ab","nonce":"n-1_x","terminal_identity":"TERM 1","ts":1730000000}')
    ps1 = open(os.path.join(os.path.dirname(da.__file__), "static", "STOIC-Installer.ps1")).read()
    # the PowerShell builder emits the same six keys, same order, compact separators
    for frag in ('\'{"capabilities":[\'', '\'"ex5_sha256":\'', '\'"installation_id":\'', '\'"nonce":\'',
                 '\'"terminal_identity":\'', '\'"ts":\' + [string][int64]$p.ts'):
        assert frag in ps1, frag


def test_valid_fresh_signature_is_admitted_and_recorded(world):
    w = world
    nonce = _challenge(w)
    p = proof(w["inst"], nonce)
    out = _attest(w, {**p, "signature": sign_rsa(w["priv"], p)})
    assert out["ok"] and out["release_match"] is True and out["key_id"] == w["dk"]["key_id"]
    inst = _run(w["db"].installations.find_one({"installation_id": w["inst"]}))
    assert inst["ex5_sha256"] == PINNED and inst["ex5_measured_by"] == "device_signature"
    assert inst["attestation"]["nonce"] == nonce and inst["attestation"]["terminal_identity"] == p["terminal_identity"]
    assert da.attested_hash(inst) == ("installer_attested", PINNED)


def test_replay_is_refused_after_single_use(world):
    w = world
    nonce = _challenge(w)
    p = proof(w["inst"], nonce)
    body = {**p, "signature": sign_rsa(w["priv"], p)}
    assert _attest(w, body)["ok"]
    with pytest.raises(da.AttestationError) as e:
        _attest(w, body)
    assert e.value.code == "nonce_invalid"


def test_tampered_fields_fail_signature_and_burn_the_nonce(world):
    w = world
    for field, bad in (("ex5_sha256", "e" * 64), ("terminal_identity", "OTHER-TERMINAL"),
                       ("capabilities", ["ex5_measured"]), ("ts", int(time.time()) - 5)):
        nonce = _challenge(w)
        p = proof(w["inst"], nonce)
        sig = sign_rsa(w["priv"], p)
        with pytest.raises(da.AttestationError) as e:
            _attest(w, {**p, field: bad, "signature": sig})
        assert e.value.code == "invalid_signature", field
        # the nonce is burnt on the first (failed) attempt — a second try with the honest payload is refused too
        with pytest.raises(da.AttestationError) as e2:
            _attest(w, {**p, "signature": sig})
        assert e2.value.code == "nonce_invalid"
    inst = _run(w["db"].installations.find_one({"installation_id": w["inst"]}))
    assert "ex5_sha256" not in inst                                   # nothing recorded
    assert _run(w["db"].attestation_failures.count_documents({"installation_id": w["inst"]})) == 4


def test_wrong_installation_nonce_and_unknown_installation(world):
    w = world
    other = f"inst_{uuid.uuid4().hex[:12]}"
    _run(w["db"].installations.insert_one({"installation_id": other, "account_id": "x", "revoked": False,
                                           "device_key": w["dk"]}))          # same key enrolled elsewhere
    nonce = _challenge(w)                                                  # nonce issued for w["inst"]
    p = proof(other, nonce)
    with pytest.raises(da.AttestationError) as e:
        _attest(w, {**p, "signature": sign_rsa(w["priv"], p)})
    assert e.value.code == "nonce_invalid"
    with pytest.raises(da.AttestationError) as e:
        _run(da.issue_challenge(w["db"], "inst_does_not_exist"))
    assert (e.value.code, e.value.status) == ("challenge_refused", 403)     # uniform refusal (r26-b P2-01)


def test_copied_proof_file_and_stolen_bridge_token_cannot_attest(world):
    """An adversary holding STOIC-Proof.txt (the public hash) + the bridge token +
    installation id but NOT the device private key."""
    w = world
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nonce = _challenge(w)
    p = proof(w["inst"], nonce)
    with pytest.raises(da.AttestationError) as e:
        _attest(w, {**p, "signature": sign_rsa(attacker, p)})
    assert e.value.code == "invalid_signature"
    # the bridge-token artifact-digest route records telemetry only → never installer_attested
    src = open(os.path.join(os.path.dirname(da.__file__), "routes", "infra_routes.py")).read()
    blk = src[src.index("async def report_artifact_digest("):src.index("async def broker_profiles_ep(")]
    assert '"ex5_reported_sha256": digest' in blk and '"ex5_sha256": digest' not in blk
    assert '"ex5_measured_by"' not in blk and '"attests": False' in blk
    inst = {"ex5_sha256": PINNED, "ex5_measured_by": "installer"}
    assert da.attested_hash(inst) == ("installer_unattested", PINNED)


def test_modified_ea_echoing_public_hash_is_not_proof():
    """Heartbeat echo without a device-signature measurement → unmeasured; with a
    signature over a DIFFERENT measured hash → installer_mismatch (bridge derivation)."""
    assert da.attested_hash({}) == ("unmeasured", None)
    assert da.attested_hash({"ex5_sha256": "f" * 64, "ex5_measured_by": "device_signature",
                             "attestation": {"verified_at": datetime.now(timezone.utc).isoformat()},
                             "device_key": {"revoked": False, "expires_at": "2999-01-01T00:00:00+00:00"}}) \
        == ("installer_attested", "f" * 64)   # bridge compares this against the heartbeat echo → mismatch


def test_stale_timestamp_malformed_signature_and_bad_payload_are_4xx_never_500(world):
    w = world
    nonce = _challenge(w)
    p = proof(w["inst"], nonce, ts=int(time.time()) - 600)
    with pytest.raises(da.AttestationError) as e:
        _attest(w, {**p, "signature": sign_rsa(w["priv"], p)})
    assert e.value.code == "stale_timestamp" and e.value.status == 401
    nonce = _challenge(w)
    p = proof(w["inst"], nonce)
    for sig in ("!!!not-base64!!!", base64.b64encode(b"short").decode(), "A" * 4097, ""):
        with pytest.raises(da.AttestationError) as e:
            _attest(w, {**p, "signature": sig})
        assert e.value.status in (401, 422)
        nonce = _challenge(w); p = proof(w["inst"], nonce)
    for bad in ({**p, "ex5_sha256": "XYZ"}, {**p, "terminal_identity": 'a"b'}, {**p, "capabilities": ["Bad Cap"]},
                {"installation_id": w["inst"]}):
        with pytest.raises(da.AttestationError) as e:
            _attest(w, {**bad, "signature": "AAAA"})
        assert e.value.code == "malformed" and e.value.status == 422


def test_revoked_and_expired_device_key_are_refused(world):
    w = world
    _run(w["db"].installations.update_one({"installation_id": w["inst"]}, {"$set": {"device_key.revoked": True}}))
    with pytest.raises(da.AttestationError) as e:
        _challenge(w)
    assert e.value.code == "challenge_refused" and e.value.status == 403        # externally uniform …
    ev = _run(w["db"].attestation_abuse.find_one({"installation_id": w["inst"]}))
    assert ev["detail"] == "device_key_revoked"                                   # … reason kept server-side
    _run(w["db"].installations.update_one({"installation_id": w["inst"]}, {"$set": {
        "device_key.revoked": False,
        "device_key.expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}}))
    with pytest.raises(da.AttestationError) as e:
        _challenge(w)
    assert e.value.code == "challenge_refused"
    # a nonce issued BEFORE revocation dies with the key
    _run(w["db"].installations.update_one({"installation_id": w["inst"]}, {"$set": {"device_key": w["dk"]}}))
    nonce = _challenge(w)
    _run(w["db"].installations.update_one({"installation_id": w["inst"]}, {"$set": {"device_key.revoked": True}}))
    p = proof(w["inst"], nonce)
    with pytest.raises(da.AttestationError) as e:
        _attest(w, {**p, "signature": sign_rsa(w["priv"], p)})
    assert e.value.code == "device_key_revoked"
    # and a previously admitted attestation degrades on the heartbeat derivation
    inst = {"ex5_sha256": PINNED, "ex5_measured_by": "device_signature",
            "attestation": {"verified_at": datetime.now(timezone.utc).isoformat()},
            "device_key": {"revoked": True, "expires_at": "2999-01-01T00:00:00+00:00"}}
    assert da.attested_hash(inst)[0] == "device_key_revoked"
    inst["device_key"]["revoked"] = False
    inst["attestation"]["verified_at"] = (datetime.now(timezone.utc) - timedelta(days=da.ATTESTATION_MAX_AGE_DAYS + 1)).isoformat()
    assert da.attested_hash(inst)[0] == "installer_attestation_stale"


def test_concurrent_submissions_of_one_nonce_admit_exactly_one(world):
    w = world
    nonce = _challenge(w)
    p = proof(w["inst"], nonce)
    body = {**p, "signature": sign_rsa(w["priv"], p)}

    async def race():
        return await asyncio.gather(*[da.verify_attestation(w["db"], dict(body)) for _ in range(6)],
                                    return_exceptions=True)
    results = _run(race())
    oks = [r for r in results if isinstance(r, dict)]
    errs = [r for r in results if isinstance(r, da.AttestationError)]
    assert len(oks) == 1 and len(errs) == 5 and all(e.code == "nonce_invalid" for e in errs)


def test_ed25519_and_pem_keys_are_accepted_and_weak_rsa_refused():
    ed = ed25519.Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(ed.public_key().public_bytes(serialization.Encoding.Raw,
                                                            serialization.PublicFormat.Raw)).decode()
    rec = da.device_key_record("Ed25519", pub_b64)
    p = proof("inst_x", "n")
    sig = base64.b64encode(ed.sign(da.canonical(p))).decode()
    da.verify_signature("Ed25519", pub_b64, sig, da.canonical(p))
    assert len(rec["key_id"]) == 16
    r = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = r.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    assert da.device_key_record("RSA-PSS-SHA256", pem)["key_id"] == da.key_id("RSA-PSS-SHA256", dotnet_xml(r.public_key()))
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    with pytest.raises(ValueError):
        da.device_key_record("RSA-PSS-SHA256", dotnet_xml(weak.public_key()))
    with pytest.raises(ValueError):
        da.device_key_record("RSA-PSS-SHA256", "<RSAKeyValue><Modulus>!!</Modulus></RSAKeyValue>")


def test_pairing_claim_enrols_the_key_and_bridge_token_path_cannot():
    src = open(os.path.join(os.path.dirname(da.__file__), "routes", "setup_routes.py")).read()
    assert "device_key: DeviceKey | None = None" in src and "da.device_key_record(" in src
    assert '"device_key": device_key,' in src and '"device_key_id": (device_key or {}).get("key_id")' in src
    infra = open(os.path.join(os.path.dirname(da.__file__), "routes", "infra_routes.py")).read()
    assert '@router.post("/attestation/challenge")' in infra and '@router.post("/attestation/verify")' in infra
    assert '@router.post("/installations/{installation_id}/device-key/revoke")' in infra
    # no route enrols a key from a bridge token
    assert "bridge_token" not in infra[infra.index("async def attestation_challenge("):infra.index("async def revoke_device_key(")]
    json.dumps(da.device_key_record("Ed25519", base64.b64encode(b"\x01" * 32).decode()))   # serialisable record


# ── r26-b P1-01: heartbeat classification denies every unusable-key reason ────────────
def _inst_fresh(**dk):
    return {"ex5_sha256": PINNED, "ex5_measured_by": "device_signature",
            "attestation": {"verified_at": datetime.now(timezone.utc).isoformat()},
            "device_key": {"revoked": False, "expires_at": "2999-01-01T00:00:00+00:00", **dk}}


def test_heartbeat_classification_denies_missing_malformed_expired_keys():
    assert da.attested_hash(_inst_fresh()) == ("installer_attested", PINNED)
    gone = _inst_fresh(); gone.pop("device_key")
    assert da.attested_hash(gone) == ("device_not_enrolled", PINNED)
    assert da.attested_hash({**_inst_fresh(), "device_key": None}) == ("device_not_enrolled", PINNED)
    assert da.attested_hash(_inst_fresh(expires_at="not-a-date")) == ("device_key_invalid", PINNED)
    bad = _inst_fresh(); bad["device_key"].pop("expires_at")
    assert da.attested_hash(bad) == ("device_key_invalid", PINNED)
    expired = _inst_fresh(expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    assert da.attested_hash(expired) == ("device_key_expired", PINNED)
    assert da.attested_hash(_inst_fresh(revoked=True)) == ("device_key_revoked", PINNED)
    src = open(da.__file__).read()
    blk = src[src.index("def attested_hash("):]
    assert '== "device_key_revoked"' not in blk and "why = _key_usable(inst.get(\"device_key\"))" in blk


def test_heartbeat_denial_reaches_the_live_gate(monkeypatch):
    monkeypatch.setenv("EA_RELEASE_SHA256", PINNED)
    from ea_capabilities import live_gate
    acc = {"mode": "live", "ea_version": "1.57", "ea_binary_sha256": PINNED}
    for method in ("device_not_enrolled", "device_key_invalid", "device_key_expired", "device_key_revoked",
                   "installer_unattested", "installer_attestation_stale"):
        assert live_gate({**acc, "ea_binary_sha256_method": method})["code"] == "EA_BINARY_PROOF_UNATTESTED", method
    assert live_gate({**acc, "ea_binary_sha256_method": "installer_attested"}) is None


# ── r26-b P2-01: challenge abuse controls ──────────────────────────────────────────────
def test_challenge_limits_single_outstanding_nonce_and_flood_alert(world, monkeypatch):
    w = world
    from fastapi import HTTPException
    # one outstanding nonce per installation: a new challenge voids the previous one
    n1 = _challenge(w); n2 = _challenge(w)
    p = proof(w["inst"], n1)
    with pytest.raises(da.AttestationError) as e:
        _attest(w, {**p, "signature": sign_rsa(w["priv"], p)})
    assert e.value.code == "nonce_invalid"
    assert _run(w["db"].attestation_nonces.find_one({"nonce": n1}))["voided_by"] == "replaced"
    p = proof(w["inst"], n2)
    assert _attest(w, {**p, "signature": sign_rsa(w["priv"], p)})["ok"]
    # per-installation limit
    monkeypatch.setattr(da, "CHALLENGE_INST_LIMIT_PER_MIN", 2)
    other = f"inst_{uuid.uuid4().hex[:12]}"
    _run(w["db"].installations.insert_one({"installation_id": other, "account_id": "x", "revoked": False, "device_key": w["dk"]}))
    _run(da.issue_challenge(w["db"], other)); _run(da.issue_challenge(w["db"], other))
    with pytest.raises(HTTPException) as e:
        _run(da.issue_challenge(w["db"], other))
    assert e.value.status_code == 429
    # per-IP limit + flood incident on enumeration
    monkeypatch.setattr(da, "CHALLENGE_IP_LIMIT_PER_MIN", 1000)
    monkeypatch.setattr(da, "FLOOD_ALERT_PER_10MIN", 3)
    for i in range(3):
        with pytest.raises(da.AttestationError):
            _run(da.issue_challenge(w["db"], f"inst_probe_{i}", client_ip="203.0.113.9"))
    inc = _run(w["db"].close_protocol_incidents.find_one({"kind": "attestation_flood", "ip": "203.0.113.9"}))
    assert inc and inc["events_10min"] >= 3
    monkeypatch.setattr(da, "CHALLENGE_INST_LIMIT_PER_MIN", 1000)
    monkeypatch.setattr(da, "CHALLENGE_IP_LIMIT_PER_MIN", 1)
    assert _run(da.issue_challenge(w["db"], w["inst"], client_ip="198.51.100.7"))["nonce"]   # 1st from this IP passes
    with pytest.raises(HTTPException) as e:
        _run(da.issue_challenge(w["db"], w["inst"], client_ip="198.51.100.7"))               # 2nd → per-IP 429
    assert e.value.status_code == 429
    src = open(os.path.join(os.path.dirname(da.__file__), "routes", "infra_routes.py")).read()
    assert "client_ip=client_ip(request)" in src
