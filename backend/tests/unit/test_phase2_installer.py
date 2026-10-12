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
    assert '$InstallerVersion = "1.10"' in ps and "[string]$Login = \"\"" in ps and "[switch]$Repair" in ps
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
    """SEC-019-001 — the .cmd is resolved from an opaque, owner-bound, 10-min download handle; the pairing token
    lives only in the file body (never in the URL)."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from fastapi import HTTPException
    import routes.setup_routes as sr
    from tests.unit.fake_mongo import FakeDb

    class _Req:
        headers = {"host": "stoicaibot.com", "x-forwarded-proto": "https"}
        url = type("U", (), {"scheme": "https", "hostname": "stoicaibot.com"})()
    import connect_service as cs
    orig_base, orig_sha, orig_db = cs._base_url, cs.installer_sha256, sr.get_db
    cs._base_url = lambda r=None: "https://stoicaibot.com"
    cs.installer_sha256 = lambda: "b" * 64
    db = FakeDb()
    sr.get_db = lambda: db
    now = datetime.now(timezone.utc)
    owner, stranger = {"id": "u1"}, {"id": "u2"}
    tok = "tok_abcdefghijklmnop"
    db.pairing_tokens.rows.append({"token": tok, "account_id": "a1", "user_id": "u1", "mt5_login": "1083440", "repair": False,
                                   "download_handle": "h_" + "a" * 20, "download_handle_expires_at": (now + timedelta(minutes=10)).isoformat()})
    db.pairing_tokens.rows.append({"token": tok + "r", "account_id": "a2", "user_id": "u1", "mt5_login": "1083440", "repair": True,
                                   "download_handle": "h_" + "b" * 20, "download_handle_expires_at": (now + timedelta(minutes=10)).isoformat()})
    db.pairing_tokens.rows.append({"token": tok + "x", "account_id": "a3", "user_id": "u1", "mt5_login": "1", "repair": False,
                                   "download_handle": "h_" + "c" * 20, "download_handle_expires_at": (now - timedelta(minutes=1)).isoformat()})
    try:
        resp = asyncio.run(sr.installer_cmd(_Req(), d="h_" + "a" * 20, user=owner))
        body = resp.body.decode()
        assert body.startswith("@echo off\r\n") and "STOIC-Setup.cmd" in resp.headers["content-disposition"]
        assert resp.headers["cache-control"] == "no-store" and resp.headers["referrer-policy"] == "no-referrer"
        assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "' in body
        assert 'Install-Stoic -Token \\"tok_abcdefghijklmnop\\" -ServerUrl \\"https://stoicaibot.com\\" -Login 1083440"' in body
        assert ("b" * 64) in body and body.rstrip().endswith("pause")
        rep = asyncio.run(sr.installer_cmd(_Req(), d="h_" + "b" * 20, user=owner)).body.decode()
        assert "-Login 1083440 -Repair" in rep
        for d, who, code in (("bad handle!", owner, 400), ("h_" + "z" * 20, owner, 404), ("h_" + "a" * 20, stranger, 404), ("h_" + "c" * 20, owner, 410)):
            with pytest.raises(HTTPException) as ei:
                asyncio.run(sr.installer_cmd(_Req(), d=d, user=who))
            assert ei.value.status_code == code, d
    finally:
        cs._base_url, cs.installer_sha256, sr.get_db = orig_base, orig_sha, orig_db
    src = _read("backend", "routes", "setup_routes.py")
    assert "user=Depends(get_current_user)" in src.split('@router.get("/setup/installer.cmd")')[1].split("\n")[1]
    assert '"cmd_download_path": f"/api/setup/installer.cmd?d={download_handle}"' in src
    assert 'rate_limit(db, "pairing_token_issue", f"user:{user[\'id\']}", 10, 900' in src
    for rel in (("frontend", "src", "components", "QuickInstallPanel.jsx"), ("frontend", "src", "components", "StatusFixDialog.jsx")):
        fe = _read(*rel)
        assert "installer.cmd?token=" not in fe and "cmd_download_path" in fe, rel
    assert "quick-install-cmd-link-" in _read("frontend", "src", "components", "QuickInstallPanel.jsx")


def test_u17_repair_button_one_line():
    qi = _read("frontend", "src", "components", "QuickInstallPanel.jsx")
    assert 'api.post("/setup/pairing-token", { account_id: accountId, repair })' in qi
    for tid in ("repair-btn-", "repair-line-", "repair-copy-", "repair-cmd-", "repair-section-"):
        assert tid in qi, tid
    assert qi.count("{repairSection}") == 1
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert '$(if ($Repair) { "Repair:" } else { "MT5 is not running:" })' in ps


def test_v110_repair_strips_saved_ea_attachments_and_stale_bridgetoken_inputs():
    """main128 Repair cleanup — with MT5 closed, every saved EmergentTradingBridge <expert> block is removed from
    profiles\\charts\\*\\chart*.chr and profiles\\templates\\*.tpl (charts stay, originals → *.stoic-bak), so the
    startup ini re-attaches exactly ONE EA and no hand-typed BridgeToken input can override the drop file."""
    import re
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert "function Remove-StoicStaleEaCharts" in ps and "[switch]$CleanCharts" in ps
    assert "if ($CleanCharts) { $null = Remove-StoicStaleEaCharts -DataFolder $DataFolder }" in ps
    assert "-CleanCharts:$Repair" in ps
    # the clean runs AFTER the graceful close and BEFORE the relaunch
    body = ps.split("function Restart-StoicTerminal")[1].split("\nfunction ")[0]
    assert body.index("WaitForExit(60000)") < body.index("Remove-StoicStaleEaCharts") < body.index("Start-Process -FilePath $Exe")
    assert "chart*.chr" in ps and "*.tpl" in ps and ".stoic-bak" in ps
    # the .NET regex the installer uses, evaluated with Python's engine (same constructs) against a real-shaped .chr
    pat = re.search(r"\$block = \[regex\]'(.+?)'\n", ps).group(1)
    rx = re.compile(pat.replace("(?is)", ""), re.I | re.S)
    chr_text = (
        "<chart>\nid=1\nsymbol=EURUSD\nperiod_type=1\n"
        "<expert>\nname=EmergentTradingBridge\npath=Experts\\EmergentTradingBridge.ex5\nexpertmode=1\n"
        "<inputs>\nServerUrl=https://old.example\nBridgeToken=abc123stale\n</inputs>\n</expert>\n"
        "<expert>\nname=OtherEA\npath=Experts\\OtherEA.ex5\n<inputs>\nLots=0.1\n</inputs>\n</expert>\n"
        "<window>\nheight=100\n</window>\n</chart>\n")
    hits = rx.findall(chr_text)
    assert len(hits) == 1 and "BridgeToken=abc123stale" in hits[0]
    clean = rx.sub("", chr_text)
    assert "EmergentTradingBridge" not in clean and "BridgeToken" not in clean
    assert "name=OtherEA" in clean and "symbol=EURUSD" in clean and "<window>" in clean   # chart + other EAs untouched
    # two STOIC blocks on one chart (the duplicate case) → both go
    dup = chr_text.replace("<expert>\nname=OtherEA", "<expert>\nname=EmergentTradingBridge\n<inputs>\n</inputs>\n</expert>\n<expert>\nname=OtherEA")
    assert len(rx.findall(dup)) == 2 and "EmergentTradingBridge" not in rx.sub("", dup)
