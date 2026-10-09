"""main113 review — N113-1 … N113-7 (paper accounts, agent BOM/ASCII, ProgramData lockdown, password gate,
watchdog skip list, seq re-sync + atomic config, anchors / per-terminal try-catch / header / icacls / auto-logon)."""
import asyncio
import os
import re
import shutil
import subprocess
import sys

import pytest
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
AGENT = os.path.join(ROOT, "backend", "static", "STOIC-Agent.ps1")


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8-sig").read()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── N113-1 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_n113_1_paper_accounts_skip_login_and_server_rules_but_keep_ini_safety():
    from models import AccountCreate
    base = {"label": "Paper", "broker": "", "server": "", "mode": "paper"}
    assert AccountCreate(**base, account_number="1001").account_number == "1001"
    assert AccountCreate(**base, account_number="SANDBOX-1").server == ""
    with pytest.raises(ValidationError):
        AccountCreate(**base, account_number="SAND[1]")                       # section syntax still refused
    with pytest.raises(ValidationError):
        AccountCreate(**{**base, "label": "a=b"}, account_number="1001")
    # non-paper keeps the A17-1 rules
    with pytest.raises(ValidationError):
        AccountCreate(label="x", broker="IC Markets", server="", account_number="52012345", mode="live")
    with pytest.raises(ValidationError):
        AccountCreate(label="x", broker="IC Markets", server="ICMarketsSC-Demo", account_number="SANDBOX-1", mode="live")
    ok = AccountCreate(label="x", broker="IC Markets", server="ICMarketsSC-Demo", account_number="52012345", mode="live")
    assert ok.server == "ICMarketsSC-Demo"


# ── N113-2 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_n113_2_agent_script_is_bom_prefixed_ascii_and_enrol_line_strips_the_bom():
    raw = open(AGENT, "rb").read()
    assert raw[:3] == b"\xef\xbb\xbf", "agent script must carry a UTF-8 BOM (Windows PowerShell 5.1 reads BOM-less files as ANSI)"
    non_ascii = [(i, b) for i, b in enumerate(raw[3:]) if b >= 0x80]
    assert not non_ascii, f"agent script must be ASCII-only, first offending bytes: {non_ascii[:5]}"
    assert b"\xe2\x80\x94" not in raw                                          # no em dash anywhere
    import vps_pathb
    line = vps_pathb.vps_agent_enrol_command("ABCD-1234")
    assert "TrimStart([char]0xFEFF)" in line and "iex $r.Content" not in line
    ci = _read(".github", "workflows", "ci.yml")
    assert "shell: powershell" in ci and "ParseFile" in ci and "has no UTF-8 BOM" in ci
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass -File backend\\static\\STOIC-Agent.ps1" in ci
    ps = _read("backend", "static", "STOIC-Agent.ps1")
    # the installer is dot-sourced from bytes with the BOM trimmed too
    assert "Invoke-Expression ([Text.Encoding]::UTF8.GetString($src.RawContentStream.ToArray()).TrimStart([char]0xFEFF))" in ps


# ── N113-3 / N113-4 / N113-5 / N113-7 (script contract) ───────────────────────────────────────────────────────────
def test_n113_3_programdata_locked_down_before_first_write_and_no_tmp_reuse():
    ps = _read("backend", "static", "STOIC-Agent.ps1")
    install = ps.split("function Install-StoicAgent")[1].split("function Test-StoicPassword")[0]
    assert install.index("Initialize-StoicDataDir") < install.index("Save-StoicAgentConfig $cfg")
    assert install.index("Invoke-StoicLockdown $script:Root") < install.index("Save-StoicAgentConfig $cfg")
    assert "[IO.Path]::GetRandomFileName()" in install and '$tmp = "$target.tmp"' not in ps
    assert "Assert-StoicTrustedOwner $target" in install
    init = ps.split("function Initialize-StoicDataDir")[1].split("# -- config")[0]
    assert "Assert-StoicTrustedOwner $script:DataDir" in init and "Invoke-StoicLockdown $script:DataDir" in init
    assert '"*.tmp"' in init and "Assert-StoicTrustedOwner $f.FullName" in init
    assert "/inheritance:r /grant:r" in ps and "/T" in ps.split("function Invoke-StoicLockdown")[1].split("}")[0]
    # icacls failure is a hard stop, not a warning (N113-7)
    assert "refusing to continue" in ps.split("function Invoke-StoicLockdown")[1].split("function Test-StoicLockedDown")[0]
    # config / logins / ledger reads refuse foreign-owned files
    assert "Assert-StoicTrustedOwner $ConfigPath" in ps and "Assert-StoicTrustedOwner $p" in ps
    assert "Initialize-StoicDataDir            # N113-3" in ps.split("function Start-StoicAgentLoop")[1]


def test_n113_4_password_gate_only_linebreak_nul_nonascii_at_entry_with_awaiting_login_fallback():
    ps = _read("backend", "static", "STOIC-Agent.ps1")
    fn = ps.split("function Test-StoicPassword")[1].split("function Set-StoicTerminalLogin")[0]
    assert r"'[\r\n\x00]'" in fn and r"'[^\x20-\x7E]'" in fn and r"\[\]=" not in fn
    setl = ps.split("function Set-StoicTerminalLogin")[1].split("function Get-StoicStoredLogin")[0]
    assert "Test-StoicPassword ([Runtime.InteropServices.Marshal]::PtrToStringUni($ptr))" in setl and "password not stored" in setl   # P1-04: no $plain variable
    inst = ps.split("function Invoke-StoicInstallTerminal")[1].split("function Invoke-StoicWatchdogTerminal")[0]
    assert 'Assert-StoicIniValue "Password"' not in inst                       # = [ ] allowed in passwords
    assert "$why = Test-StoicPassword $stored.password" in inst and "$skipWhy = $why; $stored = $null" in inst
    assert "started WITHOUT auto-login ($skipWhy)" in inst and "Test-StoicLockedDown $script:Root" in inst


def test_n113_5_and_7_watchdog_skip_list_per_terminal_try_catch_anchors_header_autologon():
    ps = _read("backend", "static", "STOIC-Agent.ps1")
    assert '@("failed", "stopped", "restart_loop", "awaiting_login", "installing", "queued")' in ps
    wd = ps.split("function Invoke-StoicWatchdog($cfg)")[1].split("function ConvertTo-StoicCanonicalParams")[0]
    assert "try { Invoke-StoicWatchdogTerminal $cfg $t } catch" in wd
    assert "$script:LoginRe = '\\A\\d{4,12}\\z'" in ps and "'^\\d{4,12}$'" not in ps
    assert re.search(r"'\^[^']*\$'", ps) is None, "regex literals must use \\A \\z, not ^ $"
    header = ps.split("param(")[0]
    assert "irm https://" not in header and "hash-pinned line" in header and "ExpectedSha256" in header
    assert "DefaultUserName" in ps and '$autoUser -ne $env:USERNAME' in ps


# ── N113-6 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
class _Agents:
    def __init__(self, agent):
        self.agent = agent

    async def update_one(self, flt, upd, **k):
        if "$max" in upd:
            for k2, v in upd["$max"].items():
                self.agent[k2] = max(int(self.agent.get(k2) or 0), v)
        return None


class _Cmds:
    def __init__(self, cmd):
        self.cmd = cmd
        self.updates = []

    async def find_one(self, flt, **k):
        return self.cmd

    async def update_one(self, flt, upd, **k):
        self.updates.append(upd)
        self.cmd.update(upd.get("$set", {}))


def test_n113_6_replay_ack_and_heartbeat_resync_server_sequence_never_rewind(monkeypatch):
    import vps_pathb
    import vps_agent

    agent = {"_id": "a", "agent_id": "agt_1", "user_id": "u1", "command_seq": 5, "last_acked_seq": 4}

    async def by_token(db, token):
        return agent
    monkeypatch.setattr(vps_agent, "agent_by_token", by_token)

    class DB:
        vps_agents = _Agents(agent)
        agent_commands = _Cmds({"_id": "c", "command_id": "cmd_1", "agent_id": "agt_1", "command": "install_terminal", "seq": 6, "status": "delivered"})
    db = DB()
    out = _run(vps_pathb.ack_command(db, "tok", "cmd_1", False, "seq_replay: agent last_seq=12", 12))
    assert out["seq_resynced"] is True and agent["command_seq"] == 12 and out["compensation"] is None
    assert db.agent_commands.cmd["status"] == "failed"
    # a lower / junk value never rewinds
    assert _run(vps_pathb._resync_command_seq(db, agent, 3)) is False and agent["command_seq"] == 12
    assert _run(vps_pathb._resync_command_seq(db, agent, "junk")) is False
    assert _run(vps_pathb._resync_command_seq(db, agent, 2**60)) is False

    # heartbeat path
    class HB(_Agents):
        async def update_one(self, flt, upd, **k):
            if "$set" in upd:
                self.agent.update(upd["$set"])
            return await super().update_one(flt, upd)
    db.vps_agents = HB(agent)
    _run(vps_agent.agent_heartbeat(db, "tok", {"last_seq": 20, "hostname": "VPS"}))
    assert agent["command_seq"] == 20
    ps = _read("backend", "static", "STOIC-Agent.ps1")
    assert 'detail = "seq_replay: agent last_seq=$lastSeq"; last_seq = $lastSeq' in ps and "last_seq = (Get-StoicLastSeq $cfg)" in ps
    assert "[IO.File]::Replace($tmp, $Path, [NullString]::Value)" in ps and "Write-StoicFileAtomic $ConfigPath" in ps
    assert "RECOVERY (N113-6)" in ps and "## Recovery (N113-6)" in _read("docs", "VPS_AGENT.md")


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
def test_n113_agent_functions_behave_under_pwsh():
    script = r'''
$ErrorActionPreference = "Stop"
. "%s"
if (Test-StoicPassword 'p=a[s]s!W0rd') { throw "symbols must be allowed" }
if (-not (Test-StoicPassword "pa`nss")) { throw "line break must be refused" }
if (-not (Test-StoicPassword ([string][char]0x00E9 + "pass"))) { throw "non-ascii must be refused" }
if ("12345678`n" -cmatch $script:LoginRe) { throw "trailing newline must not match" }
if ("12345678" -cnotmatch $script:LoginRe) { throw "plain login must match" }
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("stoic-" + [IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Path $tmp | Out-Null
$f = Join-Path $tmp "cfg.json"
Write-StoicFileAtomic $f '{"a":1}'
Write-StoicFileAtomic $f '{"a":2}'
if ((Get-Content $f -Raw) -ne '{"a":2}') { throw "atomic write lost content" }
if (@(Get-ChildItem $tmp -Force).Count -ne 1) { throw "temp file left behind" }
"PWSH-OK"
''' % AGENT
    r = subprocess.run(["pwsh", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=60)
    assert "PWSH-OK" in r.stdout, r.stdout + r.stderr


# ── A17-13 ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a17_13_real_money_refused_under_signed_demo_policy_and_consent_recorded():
    import inventory_projection as ip
    from models import AccountCreate

    class PS:
        def __init__(self, doc):
            self.doc = doc

        async def find_one(self, flt, **k):
            return self.doc

    class DB:
        def __init__(self, doc):
            self.platform_state = PS(doc)
    assert _run(ip.demo_only_policy_active(DB(None))) is False
    assert _run(ip.demo_only_policy_active(DB({"demo_only": False}))) is False
    assert _run(ip.demo_only_policy_active(DB({"demo_only": True}))) is True
    assert _run(ip.demo_only_policy_active(DB({"demo_only": True, "policy_expires_at": "2000-01-01T00:00:00+00:00"}))) is False
    a = AccountCreate(label="x", broker="IC Markets", server="ICMarketsSC-Demo", account_number="52012345",
                      declared_environment="real", algo_trading_consent=True)
    assert a.declared_environment == "real" and a.algo_trading_consent is True
    assert AccountCreate(label="x", broker="b", server="s", account_number="1234").declared_environment is None   # classic form unchanged
    routes = _read("backend", "routes", "account_routes.py")
    assert '"code": "real_refused_demo_policy"' in routes and 'payload.declared_environment == "real" and await demo_only_policy_active(db)' in routes
    assert '"algo_trading_consent_at"' in routes and '"demo_only_policy": await demo_only_policy_active(get_db())' in routes
    wz = _read("frontend", "src", "components", "AddAccountWizard.jsx")
    assert 'data-testid="wizard-env-real"' not in wz or "wizard-env-${id}" in wz
    assert "wizard-consent-checkbox" in wz and "disabled={busy || !canCreate}" in wz and "wizard-real-blocked" in wz
    assert "declared_environment: f.mode === \"paper\" ? null : f.environment" in wz
