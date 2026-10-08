"""Security audit #13 — P3 hardenings (started_at clamp, signer header drift warning)."""
import asyncio
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8-sig").read()


def test_report_terminal_clamps_future_started_at_so_grace_cannot_be_held_open():
    from test_vps_agent_service import _db, OID, NOW
    import vps_terminals as vt
    db = _db()
    agent = {"agent_id": "agt_1", "user_id": "u1", "deployment_id": "dep_1"}
    future = (NOW + timedelta(days=3650)).isoformat()
    asyncio.get_event_loop().run_until_complete(
        vt.report_terminal(db, agent, {"login": "12345678", "account_id": OID, "status": "running", "started_at": future}))
    inst = db.mt5_instances.docs[0]
    assert inst["started_at"] <= inst["updated_at"]
    db.accounts.docs[0]["last_heartbeat"] = None
    st = asyncio.get_event_loop().run_until_complete(vt.terminals_status(db, agent))
    assert st["terminals"][0]["started_age_s"] < vt.START_GRACE_S          # clamped to receipt time, not 10 years ahead
    db.mt5_instances.docs[0]["started_at"] = inst["updated_at"] - timedelta(seconds=vt.START_GRACE_S + 60)
    st = asyncio.get_event_loop().run_until_complete(vt.terminals_status(db, agent))
    assert st["terminals"][0]["restart_wanted"] is True


def test_signer_probe_warns_on_header_drift_without_blocking_release():
    probe = _read("scripts", "signer_probe.py")
    assert 'Strict-Transport-Security' in probe and 'X-Content-Type-Options' in probe and 'rec["warnings"]' in probe
    assert 'rec["result"] = "PASS" if all(c.get("ok") for c in rec["checks"].values())' in probe   # warnings are not checks
    wf = _read(".github", "workflows", "ea-release.yml")
    assert "set -o pipefail" in wf and 'signer_probe.json' in wf and '::warning::{w}' in wf


def test_audit14_agent_owner_checks_compare_well_known_sids_not_localized_names():
    ps = _read("backend", "static", "STOIC-Agent.ps1")
    assert '$script:TrustedSids = @("S-1-5-32-544", "S-1-5-18")' in ps and "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value" in ps
    assert "function ConvertTo-StoicSid" in ps and "function Test-StoicTrustedIdentity" in ps
    owner = ps.split("function Assert-StoicTrustedOwner")[1].split("function Invoke-StoicLockdown")[0]
    assert "GetOwner([Security.Principal.SecurityIdentifier])" in owner and "$script:TrustedOwners -notcontains" not in owner
    lock = ps.split("function Test-StoicLockedDown")[1].split("function Initialize-StoicDataDir")[0]
    assert lock.count("Test-StoicTrustedIdentity") == 2 and "$script:TrustedOwners -notcontains" not in lock
    assert open(os.path.join(ROOT, "backend", "static", "STOIC-Agent.ps1"), "rb").read()[:3] == b"\xef\xbb\xbf"
