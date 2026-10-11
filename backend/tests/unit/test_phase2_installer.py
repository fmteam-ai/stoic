"""Phase 2 — one-click installer (Simple Onboarding Spec U4 U5 U6 U7 U17). Installer v1.9, EA unchanged (1.64)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8-sig").read()


def test_u4_terminal_chosen_by_login_prompt_only_when_ambiguous():
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert '$InstallerVersion = "1.9"' in ps and "[string]$Login = \"\"" in ps and "[switch]$Repair" in ps
    assert "Resolve-StoicTerminal -TerminalPath $TerminalPath -TerminalId $TerminalId -ExpectedLogin $Login" in ps
    body = ps.split("function Resolve-StoicTerminal")[1].split("\nfunction ")[0]
    assert '$byLogin = @($found | Where-Object { (Get-StoicTerminalLogin $_.FullName) -eq "$ExpectedLogin" })' in body
    assert "if ($byLogin.Count -eq 1) {" in body and "return $byLogin[0]" in body
    assert body.index("if ($ExpectedLogin) {\n        $byLogin") < body.index("Get-StoicRunningTerminal -Candidates $found")   # login beats "running"
    assert "no terminal journal shows login #$ExpectedLogin" in body                                              # ambiguous → ask


def test_u4_pairing_line_carries_the_login_and_repair_flag():
    from connect_service import install_command
    line = install_command("https://stoicaibot.com", "tok_abc", "a" * 64, login="1083440")
    assert line.endswith('Install-Stoic -Token "tok_abc" -ServerUrl "https://stoicaibot.com" -Login 1083440')
    rep = install_command("https://stoicaibot.com", "tok_abc", "a" * 64, login="1083440", repair=True)
    assert rep.endswith("-Login 1083440 -Repair")
    assert install_command("https://x", "t", "a" * 64, login="not-a-login").endswith('-ServerUrl "https://x"')   # junk login dropped
    src = _read("backend", "routes", "setup_routes.py")
    assert "repair: bool = False" in src and 'install_command(base, token, sha, login=account.get("account_number")' in src
    assert 'login=acc_payload.account_number' in _read("backend", "connect_service.py")


def test_u5_webrequest_cannot_be_automated_guided_fix_with_live_check():
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert "MetaQuotes keeps that list" in ps and "encrypted" in ps                     # researched: no config-file path exists
    g = _read("frontend", "src", "components", "WebRequestGuide.jsx")
    for tid in ("webrequest-guide-", "webrequest-guide-copy-", "webrequest-guide-url-", "webrequest-guide-live-"):
        assert tid in g, tid
    assert "COPY URL" in g and "waiting for the first heartbeat" in g and "heartbeat received" in g
    assert "/WebRequest|4014|blocks the address|No request/i" in g and "!/REJECTED/i" in g     # not for a 401 (that is the token)
    ip = _read("frontend", "src", "components", "InstallProgressPanel.jsx")
    assert '<WebRequestGuide accountId={accountId} url={p.webrequest_url} step={p.steps.find((s) => s.id === "heartbeat")} />' in ip


def test_u6_mt5_started_without_prompt_when_not_running_or_repair():
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    blk = ps.split("# ── 4b. Restart MT5")[1].split("# ── 5. Summary")[0]
    assert "if (-not $isRunning -or $Repair) {" in blk
    assert blk.index("if (-not $isRunning -or $Repair)") < blk.index('Read-Host "    Restart MetaTrader 5 now? [y/N]"')   # prompt only in the else-branch
    assert "AllowLiveTrading=1" in ps and "Expert=EmergentTradingBridge" in ps                # EA attached, AutoTrading on
    assert "no input overrides: server URL, token and installation id are read from MQL5\\Files" in ps   # fresh chart, stale inputs not carried


def test_u7_double_clickable_launcher_served_per_pairing():
    import asyncio
    from fastapi import HTTPException
    import routes.setup_routes as sr

    class _Req:
        headers = {"host": "stoicaibot.com", "x-forwarded-proto": "https"}
        url = type("U", (), {"scheme": "https", "hostname": "stoicaibot.com"})()
    import connect_service as cs
    orig_base, orig_sha = cs._base_url, cs.installer_sha256
    cs._base_url = lambda r=None: "https://stoicaibot.com"
    cs.installer_sha256 = lambda: "b" * 64
    try:
        resp = asyncio.run(sr.installer_cmd(_Req(), token="tok_abcdefghijklmnop", login="1083440", repair=0))
        body = resp.body.decode()
        assert body.startswith("@echo off\r\n") and "STOIC-Setup.cmd" in resp.headers["content-disposition"]
        assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "' in body
        assert 'Install-Stoic -Token \\"tok_abcdefghijklmnop\\" -ServerUrl \\"https://stoicaibot.com\\" -Login 1083440"' in body
        assert ("b" * 64) in body and body.rstrip().endswith("pause")
        rep = asyncio.run(sr.installer_cmd(_Req(), token="tok_abcdefghijklmnop", login="1083440", repair=1)).body.decode()
        assert "-Login 1083440 -Repair" in rep
        with pytest.raises(HTTPException):
            asyncio.run(sr.installer_cmd(_Req(), token="bad token!", login="", repair=0))
    finally:
        cs._base_url, cs.installer_sha256 = orig_base, orig_sha
    qi = _read("frontend", "src", "components", "QuickInstallPanel.jsx")
    assert "/api/setup/installer.cmd?token=" in qi and "quick-install-cmd-link-" in qi


def test_u17_repair_button_one_line():
    qi = _read("frontend", "src", "components", "QuickInstallPanel.jsx")
    assert 'api.post("/setup/pairing-token", { account_id: accountId, repair })' in qi
    for tid in ("repair-btn-", "repair-line-", "repair-copy-", "repair-cmd-", "repair-section-"):
        assert tid in qi, tid
    assert qi.count("{repairSection}") == 1
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert '$(if ($Repair) { "Repair:" } else { "MT5 is not running:" })' in ps
