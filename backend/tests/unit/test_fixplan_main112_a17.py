"""main112 review + A17 audit fix list — VPS Agent hardening (N112-1 … N112-10 / A17-1 … A17-9, A17-13)."""
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.unit.test_vps_agent_service import NOW, OID, _db, _run  # noqa: E402


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8-sig").read()


def test_a17_1_account_identity_validated_at_the_boundary():
    from pydantic import ValidationError
    from models import AccountCreate
    ok = AccountCreate(label="x", broker="B", server="Broker-Demo 2", account_number=" 12345678 ")
    assert ok.account_number == "12345678"
    for bad in ({"account_number": "../12"}, {"account_number": "abc"}, {"server": "Broker\nDemo"}, {"server": "a[b]"},
                {"broker": "x=y"}, {"label": "l\r"}, {"account_number": "1" * 13}):
        with pytest.raises(ValidationError):
            AccountCreate(**{"label": "x", "broker": "B", "server": "S", "account_number": "12345678", **bad})
    routes = _read("backend", "routes", "vps_terminal_routes.py")
    assert 'pattern=r"^[A-Za-z0-9._-]{1,24}$"' in routes and "_oid_or_404" in routes


def test_a17_3_and_a17_6_install_queue_pins_installer_and_refuses_live_terminal():
    import vps_terminals as vt
    db = _db()
    db.accounts.docs[0]["last_heartbeat"] = NOW.isoformat()                       # a working terminal somewhere
    with pytest.raises(PermissionError, match="already has a live installation"):
        _run(vt.queue_install_terminal(db, {"id": "u1"}, OID, "agt_1", "https://x"))
    res = _run(vt.queue_install_terminal(db, {"id": "u1"}, OID, "agt_1", "https://x", replace=True))
    params = db.agent_commands.docs[-1]["params"]
    assert len(params["installer_sha256"]) == 64 and res["ok"]
    db = _db()
    db.mt5_instances.docs.append({"agent_id": "agt_OTHER", "account_id": OID, "account_ref": "12345678", "status": "running"})
    with pytest.raises(PermissionError, match="agt_OTHER"):
        _run(vt.queue_install_terminal(db, {"id": "u1"}, OID, "agt_1", "https://x"))
    db = _db()
    db.accounts.docs[0]["account_number"] = "../evil"
    with pytest.raises(ValueError, match="4–12 digit"):
        _run(vt.queue_install_terminal(db, {"id": "u1"}, OID, "agt_1", "https://x"))
    with pytest.raises(LookupError):
        _run(vt.queue_install_terminal(_db(), {"id": "someone"}, OID, "agt_1", "https://x"))


def test_a17_7_status_grace_and_a17_9_scoped_validated_reports(monkeypatch):
    import vps_terminals as vt
    db = _db()
    db.mt5_instances.docs.append({"agent_id": "agt_1", "account_ref": "12345678", "account_id": OID, "status": "running",
                                  "started_at": NOW - timedelta(seconds=60)})
    t = _run(vt.terminals_status(db, {"agent_id": "agt_1", "user_id": "u1"}))["terminals"][0]
    assert t["restart_wanted"] is False and t["started_age_s"] < 180                   # grace after a start
    db.mt5_instances.docs[0]["started_at"] = NOW - timedelta(seconds=400)
    assert _run(vt.terminals_status(db, {"agent_id": "agt_1", "user_id": "u1"}))["terminals"][0]["restart_wanted"] is True
    # another tenant's account is invisible to this agent
    assert _run(vt.terminals_status(db, {"agent_id": "agt_1", "user_id": "someone-else"}))["terminals"][0]["heartbeat_age_s"] is None
    agent = {"agent_id": "agt_1", "user_id": "u1", "deployment_id": "d"}
    with pytest.raises(ValueError, match="integer"):
        _run(vt.report_terminal(db, agent, {"login": "12345678", "status": "running", "restarts_last_hour": "many"}))
    with pytest.raises(ValueError, match="valid id"):
        _run(vt.report_terminal(db, agent, {"login": "12345678", "status": "running", "account_id": "nope"}))
    with pytest.raises(ValueError, match="belong"):
        _run(vt.report_terminal(db, {**agent, "user_id": "intruder"}, {"login": "12345678", "status": "running", "account_id": OID}))
    with pytest.raises(ValueError, match="digits"):
        _run(vt.report_terminal(db, agent, {"login": "../x", "status": "running"}))
    ok = _run(vt.report_terminal(db, agent, {"login": "12345678", "status": "running", "account_id": OID, "pid": "4321",
                                             "started_at": NOW.isoformat()}))
    assert ok["ok"] and db.mt5_instances.docs[0]["pid"] == 4321 and db.mt5_instances.docs[0]["started_at"] is not None


def test_a17_8_agent_offline_alert_kind_and_a17_9_poll_one_at_a_time():
    import alerting
    assert "vps_agent_offline" in alerting.EVALUATOR_KINDS and alerting.AGENT_OFFLINE_S == 300
    src = _read("backend", "alerting.py")
    assert 'raise_alert(db, "vps_agent_offline", "critical"' in src and "mt5_instances.count_documents" in src
    assert ".limit(1):" in _read("backend", "vps_pathb.py")
    routes = _read("backend", "routes", "vps_terminal_routes.py")
    assert routes.count('rate_limit(get_db(), "vps_') == 2 and '"vps_install_terminal"' in routes and '"vps_restart_terminal"' in routes
    assert 'static_error(422, "terminal_report_invalid"' in routes and 'static_error(409, "terminal_exists"' in routes
    assert '-ExpectedSha256 "{sha}"' in _read("backend", "vps_pathb.py")


def test_agent_script_hardening_contract():
    ps = _read("backend", "static", "STOIC-Agent.ps1")
    # N112-1 pinned persistent copy
    assert "[string]$ExpectedSha256" in ps and "agent script hash mismatch" in ps and "Move-Item $tmp $target -Force" in ps
    # N112-2 derived + contained paths, validated login
    assert "function Assert-StoicLogin" in ps and "function Assert-StoicUnderTerminals" in ps
    assert "$dir = Get-StoicTerminalDir $login          # N112-2" in ps and "$t.directory" not in ps.split("function Invoke-StoicWatchdog")[1].split("function Test-StoicFixedTimeEqual")[0]
    # N112-3 pin from signed params; fail if missing
    assert "command carries no installer_sha256" in ps and '$expected = "$($params.installer_sha256)".ToLower()' in ps
    assert "X-STOIC-SHA256" not in ps.split("function Invoke-StoicInstallTerminal")[1].split("function Invoke-StoicWatchdog")[0]
    # N112-4 stored server only + ini value gate
    assert "function Assert-StoicIniValue" in ps and "differs from the server stored with the password" in ps and "Server=$($stored.server)" in ps
    # N112-5 finally + sweep + ACL
    assert "finally {\n        Remove-Item $firstIni" in ps and "function Remove-StoicPasswordLeftovers" in ps and "icacls $script:Root /inheritance:r" in ps
    # N112-6 token sweep + robocopy exclude + fresh-token check
    assert '"STOIC-*.txt"' in ps and 'STOIC-*.txt") -Force' in ps and "$tokenFile.LastWriteTime -lt $installStart" in ps
    # N112-7 grace, local liveness, persisted ledger
    assert "$script:StartGraceS = 180" in ps and "$logFresh" in ps and "function Save-StoicRestartLedger" in ps and "Read-StoicRestartLedger" in ps
    # N112-9 auto-logon guidance
    assert "AutoAdminLogon" in ps and "netplwiz" in ps
    # N112-10 fail closed, replay, constant-time
    assert "refusing ALL commands (re-enrol)" in ps and "replay ignored" in ps and "function Test-StoicFixedTimeEqual" in ps
    assert "Stop-Process" not in ps
    # UI + wording
    assert "unmanaged" in _read("frontend", "src", "components", "InstallProgressPanel.jsx")
    assert '"terminal_exists"' in _read("frontend", "src", "components", "VpsAgentInstall.jsx")
    wz = _read("frontend", "src", "components", "AddAccountWizard.jsx")
    assert "REAL MT5 TERMINAL" not in wz and '"MT5 TERMINAL"' in wz
    doc = _read("docs", "VPS_AGENT.md")
    assert "auto-logon" in doc.lower() and "ExpectedSha256" in doc and "irm | iex" in doc
