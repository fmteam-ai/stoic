"""BLOCKER (v1.60.30) — Demo Autopilot never executed on MT5 demo accounts: operational_modes.mode_gate blocked
demo_autopilot whenever account["mode"] == "live", but `mode` is live-vs-PAPER, so every MT5 account (attested DEMO
included) was "live". The gate now uses broker_env.attested_environment: attested DEMO / PAPER execute, LIVE never."""
import os
import sys
from datetime import datetime, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit


def _mt5_demo():
    """Attested RoboForex DEMO as saved on prod: mode 'live' (= real MT5 terminal), EA reports trade_mode demo,
    admin/auto attestation bound to the current identity digest."""
    from broker_env import attestation_identity
    now = datetime.now(timezone.utc).isoformat()
    a = {"_id": ObjectId(), "user_id": "u1", "mode": "live", "broker": "RoboForex", "server": "RoboForex-ECN",
         "account_number": "1083440", "account_type": "microcent", "broker_server": "RoboForex-ECN",
         "broker_account_id_reported": "1083440", "broker_environment": "DEMO", "last_heartbeat": now,
         "verified_identity": {"account_number": "1083440"},
         "ea_identity": {"authoritative": True, "installation_id": "inst-1", "trade_mode": "demo", "broker_server": "RoboForex-ECN"}}
    a["environment_attestation"] = {"environment": "DEMO", "approved_by": "admin@x", "identity_hash": attestation_identity(a),
                                    "proof": {"verifier": "ea_heartbeat"}}
    return a


def test_attested_demo_mt5_account_executes_in_demo_autopilot():
    from broker_env import attested_environment
    from operational_modes import MODES, mode_gate
    acc = _mt5_demo()
    assert acc["mode"] == "live" and attested_environment(acc) == "DEMO"          # the exact prod shape
    g = mode_gate({"operational_mode": "demo_autopilot"}, acc)
    assert g["allow_new"] is True and g["lot_scale"] == MODES["demo_autopilot"]["lot_scale"] and g["mode"] == "demo_autopilot"
    assert "do not execute" not in g["reason"]


def test_live_account_never_executes_in_demo_autopilot():
    from operational_modes import mode_gate
    # 1 · no attestation at all → LIVE
    live = _mt5_demo()
    live.pop("environment_attestation")
    g = mode_gate({"operational_mode": "demo_autopilot"}, live)
    assert g["allow_new"] is False and g["lot_scale"] == 0.0 and "LIVE" in g["reason"]
    # 2 · attested DEMO but the broker now reports real money → attestation void → LIVE → blocked (N98-6)
    voided = _mt5_demo()
    voided["ea_identity"]["trade_mode"] = "real"
    assert mode_gate({"operational_mode": "demo_autopilot"}, voided)["allow_new"] is False
    # 3 · attestation bound to a different identity (credentials / server changed) → LIVE → blocked
    rebound = _mt5_demo()
    rebound["environment_attestation"]["identity_hash"] = "stale"
    assert mode_gate({"operational_mode": "demo_autopilot"}, rebound)["allow_new"] is False
    # 4 · no account context → fail closed
    assert mode_gate({"operational_mode": "demo_autopilot"}, {})["allow_new"] is False


def test_paper_account_and_other_modes_are_unchanged():
    from operational_modes import MODES, mode_gate
    assert mode_gate({"operational_mode": "demo_autopilot"}, {"mode": "paper"})["allow_new"] is True
    live = _mt5_demo()
    live.pop("environment_attestation")
    for mode, spec in MODES.items():
        if spec.get("demo_only"):
            continue
        g = mode_gate({"operational_mode": mode}, live)
        assert g["allow_new"] == spec["allow_new"] and g["lot_scale"] == spec["lot_scale"], mode
    assert mode_gate({"operational_mode": "nonsense"}, live)["allow_new"] is False


def test_gate_source_uses_attested_environment_not_mode_field():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "operational_modes.py"),
               encoding="utf-8").read()
    block = src.split("def mode_gate")[1].split("async def record_intercept")[0]
    assert "attested_environment(account or {})" in block and 'env not in ("DEMO", "PAPER")' in block
    assert '.get("mode") or "").lower() == "live"' not in block
