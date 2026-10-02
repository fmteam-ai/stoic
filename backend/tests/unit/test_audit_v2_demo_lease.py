"""Audit v2 P1-04 — a DEMO environment attestation is a lease that holds only
while the terminal keeps proving it; a lapsed lease collapses to LIVE."""
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

import broker_env
from broker_env import attestation_identity, attestation_state, attested_environment
from ea_capabilities import live_gate

pytestmark = pytest.mark.unit


def _iso(**ago):
    return (datetime.now(timezone.utc) - timedelta(**ago)).isoformat()


def _attested(**over):
    acc = {"_id": ObjectId(), "mode": "live", "account_type": "demo", "broker": "VT Markets",
           "server": "VTMarkets-Demo", "broker_server": "VTMarkets-Demo",
           "account_number": "1289887", "ea_version": "1.58", "creds_version": 0,
           "broker_account_id_reported": "1289887", "last_heartbeat": _iso(seconds=5),
           "ea_identity": {"installation_id": "inst-A", "authoritative": True,
                           "broker_server": "VTMarkets-Demo"}}
    acc.update(over)
    acc["environment_attestation"] = {
        "environment": "DEMO", "approved_by": "adm@example.com", "at": _iso(minutes=1),
        "identity_hash": attestation_identity(acc),
        "proof": {"verifier": "ea_heartbeat", "proof_id": "p"}}
    return acc


def test_fresh_demo_lease_is_demo():
    acc = _attested()
    assert attested_environment(acc) == "DEMO"
    assert attestation_state(acc) == "valid"
    assert live_gate(acc) is None


def test_stale_heartbeat_collapses_to_live_and_blocks_live_gates():
    acc = _attested()
    acc["last_heartbeat"] = _iso(seconds=broker_env.DEMO_LEASE_MAX_HEARTBEAT_AGE_S + 60)
    assert attested_environment(acc) == "LIVE"
    assert attestation_state(acc) == "lapsed"
    gate = live_gate(acc)
    assert gate is not None and gate["code"] in (
        "EA_DEMO_LEASE_LAPSED", "EA_RELEASE_HASH_UNPINNED", "EA_BINARY_PROOF_MISSING")


def test_missing_heartbeat_is_not_demo():
    acc = _attested()
    acc.pop("last_heartbeat")
    assert attested_environment(acc) == "LIVE"


def test_expired_approval_lapses(monkeypatch):
    acc = _attested()
    acc["environment_attestation"]["at"] = _iso(days=broker_env.DEMO_ATTESTATION_MAX_AGE_DAYS + 1)
    assert attested_environment(acc) == "LIVE"
    assert attestation_state(acc) == "lapsed"
    monkeypatch.setattr(broker_env, "DEMO_ATTESTATION_MAX_AGE_DAYS", 0)   # 0 disables expiry
    assert attested_environment(acc) == "DEMO"


def test_lease_recovers_when_heartbeat_fresh_again():
    acc = _attested()
    acc["last_heartbeat"] = _iso(hours=2)
    assert attested_environment(acc) == "LIVE"
    acc["last_heartbeat"] = _iso(seconds=1)
    assert attested_environment(acc) == "DEMO"


def test_identity_drift_still_invalidates():
    acc = _attested()
    acc["server"] = "VTMarkets-Live"
    assert attested_environment(acc) == "LIVE"
    assert attestation_state(acc) == "invalidated"
