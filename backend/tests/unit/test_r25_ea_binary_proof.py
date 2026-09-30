"""r25 P1-01 — EA binary proof chain (AT-P1-01 decision matrix).

The live gate admits a terminal only when the heartbeat-reported EX5 hash
(a) equals the signed release hash and (b) arrived as `installer_attested`,
i.e. equals the hash the STOIC installer measured on the deployed EX5 for the
same installation. Everything else is a canonical blocker."""
import re
import pytest

import ea_capabilities as ec

GOOD = "a" * 64
OTHER = "b" * 64


@pytest.fixture(autouse=True)
def pin_release(monkeypatch):
    monkeypatch.setattr(ec, "expected_ea_sha256", lambda: GOOD)


def _acc(**kw):
    base = {"ea_version": "1.57", "mode": "live"}
    base.update(kw)
    return base


@pytest.mark.parametrize("account, code", [
    (_acc(ea_binary_sha256=GOOD, ea_binary_sha256_method="installer_attested"), None),
    (_acc(), "EA_BINARY_PROOF_MISSING"),                                                   # no hash at all
    (_acc(ea_binary_sha256=OTHER, ea_binary_sha256_method="installer_attested"), "EA_BINARY_HASH_MISMATCH"),  # modified EX5
    (_acc(ea_binary_sha256=GOOD, ea_binary_sha256_method="user_trust"), "EA_BINARY_PROOF_UNATTESTED"),        # one-click terminal echoing the hash
    (_acc(ea_binary_sha256=GOOD, ea_binary_sha256_method="installer_mismatch"), "EA_BINARY_PROOF_UNATTESTED"),  # EA echoes release hash, installer measured a different file
    (_acc(ea_binary_sha256=GOOD, ea_binary_sha256_method="installer"), "EA_BINARY_PROOF_UNATTESTED"),   # paired but never measured
    (_acc(ea_binary_sha256=GOOD, ea_binary_sha256_method="unknown"), "EA_BINARY_PROOF_UNATTESTED"),
    (_acc(ea_binary_sha256=GOOD, ea_binary_sha256_method="installer_attested", ea_version="1.56"), "EA_CAPABILITY_BELOW_MIN"),
    (_acc(ea_binary_sha256=GOOD, ea_binary_sha256_method="installer_attested", mode="paper"), None),   # paper never needs proof
])
def test_live_gate_matrix(account, code):
    res = ec.live_gate(account)
    assert (res or {}).get("code") == code, res


def test_unpinned_release_blocks_everything(monkeypatch):
    monkeypatch.setattr(ec, "expected_ea_sha256", lambda: "")
    assert ec.live_gate(_acc(ea_binary_sha256=GOOD, ea_binary_sha256_method="installer_attested"))["code"] == "EA_RELEASE_HASH_UNPINNED"


def test_bridge_derives_method_from_installer_measurement():
    src = open("routes/bridge_routes.py").read()
    block = src[src.index("r25 P1-01: `installer_attested`"):src.index("elif not (identity and identity[\"ok\"])")]
    assert '"installer_attested"' in block and '"installer_mismatch"' in block
    assert 'measured == reported_hash and measured_method == "installer_attested"' in block
    assert "da.attested_hash(inst_doc)" in block      # r26 P1-02: method comes from the signed attestation
    assert '"ex5_measured_by": 1' in src and '"attestation": 1' in src


def test_installer_and_ea_carry_the_proof():
    ps1 = open("static/STOIC-Installer.ps1").read()
    assert "STOIC-Proof.txt" in ps1 and "Get-FileHash $finalEx5 -Algorithm SHA256" in ps1
    assert "installation_id = $installationId" in ps1 and "/api/infra/attestation/verify" in ps1
    assert "RSASignaturePadding]::Pss" in ps1 and "ProtectedData]::Protect" in ps1     # r26 P1-02 signed proof
    mq5 = open("static/EmergentTradingBridge.mq5").read()
    assert 'FileIsExist("STOIC-Proof.txt")' in mq5
    assert re.search(r'\\"ea_binary_sha256\\":\\"%s\\"', mq5), "heartbeat must carry ea_binary_sha256"
    # format args order: ... EA_CLIENT_VERSION, EffectiveProofHash, TimeGMT
    assert re.search(r"EA_CLIENT_VERSION,\s*EffectiveProofHash,\s*\(long\)TimeGMT", mq5)


def test_digest_endpoint_binds_installation():
    src = open("routes/infra_routes.py").read()
    # r26 P1-02: the bridge-token digest report is telemetry (ex5_reported_*) — it never writes the
    # admissible measurement (ex5_sha256 / ex5_measured_by), which only the signed attestation records
    assert '"ex5_reported_sha256": digest' in src and '"installation_id": inst_id, "account_id": reporter["account_id"]' in src
    assert '"ex5_sha256": digest' not in src
    assert 'reporter["kind"] == "installer"' in src     # agent tokens can't bind someone else's installation
