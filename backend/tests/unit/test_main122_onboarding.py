"""main122 — live onboarding fixes (OnEquity demo #1083440, installer v1.7 → 1.8, EA 1.62 → 1.63).

P1  BOM in STOIC-*.txt (Windows PowerShell 5.1 `Set-Content -Encoding UTF8`) → EA sent the '#' comment line as the
    token → 401 on every heartbeat. Installer writes without BOM; EA strips a BOM and validates the token /
    installation / proof shape; CI parses 5.1-written files with the EA's exact reader (scripts/ea_dropfile_reader.py).
P2  Broker server of an account can be corrected (PATCH server · adopt-reported-server · server-suggest).
P2  Diagnostics: 401 heartbeats are reported as REJECTED (not "WebRequest URL missing"); EA logs non-2xx; the
    mismatch panel no longer suggests re-pairing; the backend logs the 401 reason.
P3  One canonical host: https://stoicaibot.com everywhere (EA compiled default = installer = panel)."""
import asyncio
import importlib.util
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8-sig").read()


def _reader():
    spec = importlib.util.spec_from_file_location("ea_dropfile_reader", os.path.join(ROOT, "scripts", "ea_dropfile_reader.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TOKEN = "k3XbQ2m9Vp0sT7wLzA4cR8yE1nH6uJ5gD2fK9qW0xZs"   # 43 chars — secrets.token_urlsafe(32)
BOM = b"\xef\xbb\xbf"
OLD_TOKEN_FILE = BOM + b"# STOIC bridge token \xe2\x80\x94 keep this file confidential.\r\n# Account: OnEquity #1083440 (demo)\r\n" + TOKEN.encode() + b"\r\n"


# ── P1 · EA reader ───────────────────────────────────────────────────────────────────────────────────
def test_p1_ea_strips_bom_and_validates_every_drop_file():
    mq5 = _read("backend", "static", "EmergentTradingBridge.mq5")
    assert '#define EA_CLIENT_VERSION "1.63"' in mq5 and '#property version   "1.63"' in mq5
    assert "string StripBom(string s)" in mq5 and "0xFEFF" in mq5 and ">= 0x80" in mq5
    # every reader goes through the BOM-tolerant line read
    assert mq5.count("StripBom(FileReadString(fh))") == 2          # ReadDropFileValue + ResolveServerUrl
    for fn in ("ResolveProofHash", "ResolveInstallationId", "ResolveBridgeToken"):
        body = mq5.split(f"string {fn}()")[1].split("\n}\n")[0]
        assert "ReadDropFileValue(" in body, fn
        assert "FileOpen(" not in body, f"{fn} must not open the file itself (no BOM handling)"
    # shape validation: 32-128 of A-Za-z0-9_- (a range, not an exact length), inst_ + 12 hex, 64 hex
    assert "bool IsTokenShape(string t)" in mq5 and "n < 32 || n > 128" in mq5
    assert "bool IsInstallationShape(string s)" in mq5 and 'StringFind(s, "inst_") != 0' in mq5 and "StringLen(s) != 17" in mq5
    assert "bool IsHex64(string s)" in mq5
    tok = mq5.split("string ResolveBridgeToken()")[1].split("\n}\n")[0]
    assert "token file malformed" in tok and "if (!IsTokenShape(token))" in tok and "Heartbeats NOT sent" in tok
    assert "if (!IsTokenShape(input_trim))" in tok                   # pasted inputs validated too
    inst = mq5.split("string ResolveInstallationId()")[1].split("\n}\n")[0]
    assert "installation file malformed" in inst and "if (!IsInstallationShape(inst))" in inst
    proof = mq5.split("string ResolveProofHash()")[1].split("\n}\n")[0]
    assert "if (!IsHex64(h))" in proof
    srv = mq5.split("string ResolveServerUrl()")[1].split("int OnInit()")[0]
    assert "StringGetCharacter(line, 0) == '#') continue;" in srv


def test_p1_reader_port_matches_ea_semantics(tmp_path):
    r = _reader()
    # BOM read as ANSI (cp1252) → "ï»¿# STOIC …": the 1.63 reader recovers the token, the legacy one takes the comment
    text = r.decode_ansi(OLD_TOKEN_FILE)
    assert text.startswith("ï»¿#")
    assert r.read_token(text) == {"value": TOKEN, "ok": True, "reason": None}
    legacy = r.read_token(text, legacy=True)
    assert legacy["ok"] and legacy["value"].startswith("ï»¿# STOIC") and legacy["value"] != TOKEN   # the production failure
    # U+FEFF form (file read as unicode) is stripped too
    assert r.strip_bom("\ufeff# x") == "# x" and r.strip_bom("abc") == "abc" and r.strip_bom("ï»¿abc") == "abc"
    # shape rules
    assert not r.read_token("# only\n\n")["ok"]
    assert r.read_token("a" * 32)["ok"] and r.read_token("a" * 128)["ok"]
    assert not r.read_token("a" * 31)["ok"] and not r.read_token("a" * 129)["ok"] and not r.read_token("a" * 40 + "!")["ok"]
    bad = r.read_token("# STOIC bridge token — keep this file confidential.\n", legacy=False)
    assert not bad["ok"] and bad["value"] == ""
    assert r.read_installation("inst_0123456789ab")["ok"] and not r.read_installation("inst_0123456789a")["ok"]
    assert not r.read_installation("# STOIC installation id — proves WHICH EA install is talking.")["ok"]
    assert r.read_proof("A" * 64)["value"] == "a" * 64 and not r.read_proof("g" * 64)["ok"]
    assert r.read_server("# c\nhttps://stoicaibot.com/\n")["value"] == "https://stoicaibot.com"
    assert r.read_server("http://x.example\n")["value"] == r.SERVER_URL_DEFAULT == "https://stoicaibot.com"
    # directory walk + CLI expectations
    d = tmp_path / "Files"
    d.mkdir()
    (d / "STOIC-Token.txt").write_bytes(OLD_TOKEN_FILE)
    (d / "STOIC-Server.txt").write_bytes(b"# c\r\nhttps://stoicaibot.com\r\n")
    (d / "STOIC-Installation.txt").write_bytes(b"# c\r\ninst_0123456789ab\r\n")
    res = r.read_dir(d)
    assert res["token"]["bom"] is True and res["token"]["value"] == TOKEN
    assert res["server"]["bom"] is False and res["proof"]["reason"] == "missing"
    assert r.main(["--files-dir", str(d), "--expect-token", TOKEN, "--expect-server", "https://stoicaibot.com"]) == 0
    assert r.main(["--files-dir", str(d), "--expect-no-bom"]) == 1          # BOM file → gate fails
    assert r.main(["--files-dir", str(d), "--legacy", "--expect-token", TOKEN]) == 1


def test_p1_reader_is_host_python_39_compatible():
    src = _read("scripts", "ea_dropfile_reader.py")
    assert "from __future__ import annotations" in src
    assert "match " not in src and ":=" not in src


# ── P1 · installer writer ────────────────────────────────────────────────────────────────────────────
def test_p1_installer_writes_drop_files_without_bom():
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert '$InstallerVersion = "1.8"' in ps
    assert "function Write-StoicDropFile" in ps and "New-Object System.Text.UTF8Encoding($false)" in ps
    assert "[IO.File]::WriteAllText($Path, $text, (New-Object System.Text.UTF8Encoding($false)))" in ps
    body = ps[ps.index("function Install-Stoic"):]
    for ln in body.splitlines():
        if "STOIC-Token.txt" in ln or "STOIC-Server.txt" in ln or "STOIC-Installation.txt" in ln:
            assert "Set-Content" not in ln and "-Encoding UTF8" not in ln and "Out-File" not in ln, ln
    assert body.count("Write-StoicDropFile -Path") == 3                  # token, server, installation
    assert "-Encoding UTF8 " not in body and not body.rstrip().endswith("-Encoding UTF8")
    assert 'Set-Content -Path (Join-Path $filesDir "STOIC-Proof.txt") -Value $proofHash -Encoding ASCII' in ps   # unchanged, ASCII has no BOM
    assert "function Test-StoicDropFileNoBom" in ps
    assert ps.startswith("# STOIC-Installer.ps1")   # the installer ITSELF keeps its BOM (read with utf-8-sig)


def test_p1_ci_runs_the_writer_under_windows_powershell_51_and_parses_with_the_ea_reader():
    ci = _read(".github", "workflows", "ci.yml")
    step = ci.split("Drop-file encoding gate under Windows PowerShell 5.1")[1].split("- name:")[0]
    assert "shell: powershell" in step and "powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\\test_installer_dropfiles.ps1" in step
    assert "PSVersion.Major -ne 5" in step
    t = _read("scripts", "test_installer_dropfiles.ps1")
    assert "PSVersion.Major -ne 5" in t and "Write-StoicDropFile" in t and "ea_dropfile_reader.py" in t
    assert "--expect-no-bom" in t and "--expect-token $token" in t and "--legacy" in t      # positive + negative control
    assert "Set-Content -Path (Join-Path $old \"STOIC-Token.txt\") -Encoding UTF8" in t     # the pre-1.8 writer reproduced
    raw = open(os.path.join(ROOT, "scripts", "test_installer_dropfiles.ps1"), "rb").read()
    assert raw[:3] == BOM, "5.1 reads a BOM-less script as ANSI — the em dashes in strings would be mangled"


# ── P2 · broker server correction ───────────────────────────────────────────────────────────────────
def test_p2_server_edit_resets_the_chain_and_is_step_up_gated():
    import step_up
    from routes import account_routes as ar
    assert "account_server_edit" in step_up.STEP_UP_ACTIONS
    assert ar.AccountUpdate(server=" OnEquity-Server ").server.strip() == "OnEquity-Server"
    acc = {"_id": "x", "server": "OnEquity-Demo", "expected_identity": {"account_number": "1083440", "broker_server": "OnEquity-Demo"},
           "verified_identity": {"broker_server": "OnEquity-Demo"}, "broker_account_mismatch": True}
    doc = ar._server_change_doc(acc, "OnEquity-Server", source="owner_edit", by="u1")
    s = doc["$set"]
    assert s["server"] == s["broker_server"] == s["expected_identity"]["broker_server"] == "OnEquity-Server"
    assert s["expected_identity"]["account_number"] == "1083440"                 # the rest of the claim is kept
    assert s["broker_account_mismatch"] is False and s["broker_account_mismatch_reason"] is None
    assert doc["$unset"] == {"verified_identity": ""}                              # chain re-verifies on the next heartbeat
    assert s["server_changed"]["from"] == "OnEquity-Demo" and s["server_changed"]["source"] == "owner_edit"
    for ok in ("OnEquity-Server", "ICMarketsSC-Demo02", "VTMarkets-Live 2", "RoboForex-ECN", "JMFinancial-Server"):
        assert re.match(ar.SERVER_NAME_RE, ok), ok
    for bad in ("", "x", "-lead", "a" * 65, "evil;drop", "OnEquity/Server"):
        assert not re.match(ar.SERVER_NAME_RE, bad), bad
    src = _read("backend", "routes", "account_routes.py")
    patch = src.split('@router.patch("/{account_id}")')[1].split("@router.")[0]
    assert 'require_step_up(db, user, request, "account_server_edit")' in patch and '"account_server_edited"' in patch
    assert 'acc.get("mode") == "paper"' in patch
    adopt = src.split('@router.post("/{account_id}/adopt-reported-server")')[1].split("@router.")[0]
    assert 'require_step_up(db, user, request, "account_server_edit")' in adopt and '"account_server_adopted_from_ea"' in adopt
    assert '"login_differs"' in adopt                                               # only the SAME login's server can be adopted
    assert "servers_match(acc.get(\"server\"), reported)" in adopt                 # alias-equal → no-op, no MFA prompt


def test_p2_onequity_server_in_registry_and_server_suggest_logic():
    from broker_presets import BROKER_PRESETS
    one = next(p for p in BROKER_PRESETS if p["broker"] == "OnEquity")
    assert one["servers"][0] == "OnEquity-Server" and "OnEquity-Demo" in one["servers"]
    from routes import account_routes as ar

    class _Cur:
        def __init__(self, docs): self._d = docs
        def limit(self, n): return self
        def __aiter__(self):
            async def gen():
                for d in self._d:
                    yield d
            return gen()

    class _Coll:
        def find(self, *a, **k): return _Cur([{"broker": "OnEquity", "server_names": ["OnEquity-Real2"]}])

    class _DB:
        broker_profiles = _Coll()

    import routes.account_routes as mod
    orig = mod.get_db
    mod.get_db = lambda: _DB()
    try:
        known = asyncio.run(ar.server_suggest(server="onequity-server", broker="OnEquity", user={"id": "u"}))
        assert known["known"] is True and known["hint"] is None
        typo = asyncio.run(ar.server_suggest(server="OnEquity-Dem", broker="OnEquity", user={"id": "u"}))
        assert typo["known"] is False and "EXACT name MT5 shows" in typo["hint"]
        assert {"OnEquity-Server", "OnEquity-Demo", "OnEquity-Real2"} <= set(typo["suggestions"])
        alias = asyncio.run(ar.server_suggest(server="OnEquity-Real", broker="OnEquity", user={"id": "u"}))
        assert alias["known"] is True                                              # alias group onequity-live/onequity-real
        empty = asyncio.run(ar.server_suggest(server="", broker="", user={"id": "u"}))
        assert empty["known"] is False and empty["hint"] is None
    finally:
        mod.get_db = orig


def test_p2_wizard_validates_the_typed_server_and_allows_an_exact_name():
    wz = _read("frontend", "src", "components", "AddAccountWizard.jsx")
    assert "/accounts/server-suggest" in wz and 'data-testid="wizard-server-hint"' in wz
    assert '<option value="__custom">Other — type the exact MT5 server name</option>' in wz
    assert "wizard-server-suggestion-" in wz and 'data-testid="wizard-server-known"' in wz


# ── P2 · diagnostics ────────────────────────────────────────────────────────────────────────────────
def _acc(**kw):
    base = {"_id": "a1", "account_number": "1083440", "installer_paired_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            "installer_version": "1.7", "installer_paired_hostname": "VPS-1"}
    base.update(kw)
    return base


def test_p2_install_progress_says_rejected_not_webrequest_missing():
    import install_progress as ip
    now = datetime.now(timezone.utc)
    rej = {"at": (now - timedelta(seconds=20)).isoformat(), "reason": "invalid_bridge_token", "token_len": 56, "ea_version": "1.62"}
    out = ip.derive(_acc(bridge_rejected=rej), None, None, attested=(None, None), accepted_hashes=[], now=now)
    hb = next(s for s in out["steps"] if s["id"] == "heartbeat")
    assert hb["status"] == "blocked"
    assert hb["detail"].startswith("Heartbeats arriving but REJECTED: invalid bridge token (401)")
    assert "token length 56" in hb["detail"] and "EA 1.62" in hb["detail"]
    assert "WebRequest is NOT the problem" in hb["hint"] and "BOM" in hb["hint"] and "v1.8" in hb["hint"] and "1.63" in hb["hint"]
    assert "56 characters long" in hb["hint"]
    assert "Most likely the WebRequest URL is missing" not in hb["hint"]
    assert out["bridge_rejected"] == rej and out["state"] == "blocked"
    # stale mark (older than BRIDGE_REJECTED_FRESH_S) → the old "no heartbeat since pairing" path again
    old = dict(rej, at=(now - timedelta(seconds=ip.BRIDGE_REJECTED_FRESH_S + 5)).isoformat())
    out2 = ip.derive(_acc(bridge_rejected=old), None, None, attested=(None, None), accepted_hashes=[], now=now)
    hb2 = next(s for s in out2["steps"] if s["id"] == "heartbeat")
    assert "REJECTED" not in hb2["detail"] and out2["bridge_rejected"] is None
    # a VALID heartbeat newer than the mark wins (token fixed, EA re-attached)
    out3 = ip.derive(_acc(bridge_rejected=rej, last_heartbeat=now.isoformat()), None, None, attested=(None, None), accepted_hashes=[], now=now)
    hb3 = next(s for s in out3["steps"] if s["id"] == "heartbeat")
    assert hb3["status"] == "done"
    # reason-specific hints
    assert "suspended" in ip._rejection_hint({"reason": "bridge_token_suspended"})
    assert "rotated away" in ip._rejection_hint({"reason": "bridge_token_retired"})


def test_p2_identity_hint_does_not_suggest_repair_for_a_server_mismatch():
    import install_progress as ip
    now = datetime.now(timezone.utc)
    ident = {"installation_id": "inst_0123456789ab", "authoritative": False,
             "reason": "broker server mismatch: EA reports 'OnEquity-Server', account is registered on 'OnEquity-Demo' (exact alias-registry match required)"}
    out = ip.derive(_acc(ea_identity=ident, last_heartbeat=now.isoformat()), None, None, attested=(None, None), accepted_hashes=[], now=now)
    idn = next(s for s in out["steps"] if s["id"] == "identity")
    assert idn["status"] == "blocked" and "Re-pair" not in idn["hint"] and "Use the server the EA reports" in idn["hint"]
    other = dict(ident, reason="installation is bound to a different account")
    out2 = ip.derive(_acc(ea_identity=other, last_heartbeat=now.isoformat()), None, None, attested=(None, None), accepted_hashes=[], now=now)
    assert "Re-pair" in next(s for s in out2["steps"] if s["id"] == "identity")["hint"]


def test_p2_heartbeat_401_marks_the_login_and_logs_the_reason():
    from fastapi import HTTPException
    from routes import bridge_routes as br
    src = _read("backend", "routes", "bridge_routes.py")
    assert 'logger.info("bridge 401 reason=suspended account=%s", acc["_id"])' in src
    assert 'logger.info("bridge 401 reason=%s account=%s token_len=%d token_prefix=%s"' in src
    assert '"code": "bridge_token_retired" if retired else "invalid_bridge_token"' in src
    hb = src.split('@router.post("/heartbeat")')[1].split("@router.")[0]
    assert "_record_heartbeat_rejection(db, payload, _rej)" in hb and '{"$unset": {"bridge_rejected": ""}}' in hb
    assert br._rejection_code(HTTPException(401, detail={"code": "bridge_token_suspended"})) == "bridge_token_suspended"
    assert br._rejection_code(HTTPException(401, detail="Invalid bridge token")) == "invalid_bridge_token"

    calls = []

    class _Coll:
        async def update_many(self, q, u): calls.append((q, u))

    class _DB:
        accounts = _Coll()

    class _P:
        account_login = 1083440
        bridge_token = "# STOIC bridge token — keep this file confidential."
        ea_version = "1.62"
        broker_server = "OnEquity-Server"

    br._REJECT_WRITTEN.clear()
    asyncio.run(br._record_heartbeat_rejection(_DB(), _P(), HTTPException(401, detail={"code": "invalid_bridge_token"})))
    assert len(calls) == 1
    q, u = calls[0]
    assert q == {"account_number": "1083440", "mode": {"$ne": "paper"}}
    mark = u["$set"]["bridge_rejected"]
    assert mark["reason"] == "invalid_bridge_token" and mark["token_len"] == 51 and mark["ea_version"] == "1.62"
    assert "token" not in {k for k in mark if k != "token_len"}                   # no secret material in the mark
    asyncio.run(br._record_heartbeat_rejection(_DB(), _P(), HTTPException(401, detail={"code": "invalid_bridge_token"})))
    assert len(calls) == 1                                                        # throttled (30 s per login)

    class _NoLogin(_P):
        account_login = None

    br._REJECT_WRITTEN.clear()
    asyncio.run(br._record_heartbeat_rejection(_DB(), _NoLogin(), HTTPException(401, detail="x")))
    assert len(calls) == 1                                                        # nothing to attribute → no write


def test_p2_ea_logs_non_2xx_answers_throttled():
    mq5 = _read("backend", "static", "EmergentTradingBridge.mq5")
    post = mq5.split("string HttpPost(string url, string body)")[1].split("\n}\n")[0]
    assert "if (res < 200 || res >= 300) LogHttpRejected(url, res, answer);" in post
    log = mq5.split("void LogHttpRejected(string url, int status, string body)")[1].split("\n}\n")[0]
    assert "TimeCurrent() - _last_http_err_at < 60" in log                       # once per minute per status
    assert 'Print("STOIC: ", endpoint, " rejected HTTP ", status, " ", code, hint);' in log
    assert 'code = "invalid_bridge_token"' in log and 're-run the STOIC installer (v1.8+)' in log
    assert "string JsonStringField(string body, string key)" in mq5


def test_p2_frontend_mismatch_copy_and_fix_panel():
    acc = _read("frontend", "src", "pages", "Accounts.jsx")
    assert "BROKER SERVER MISMATCH" in acc and "/broker server mismatch/i" in acc
    assert "re-pairing does not change it" in acc
    assert 'const LATEST_EA_VERSION = "1.63";' in acc
    fix = _read("frontend", "src", "components", "ServerMismatchFix.jsx")
    assert "/adopt-reported-server" in fix and "USE THE SERVER THE EA REPORTS" in fix
    assert "api.patch(`/accounts/${account.id}`, { server: server.trim() })" in fix
    assert "adopt-reported-server-" in fix and "edit-server-save-" in fix
    qi = _read("frontend", "src", "components", "QuickInstallPanel.jsx")
    assert qi.count("<ServerMismatchFix account={account} onFixed={onTrusted} />") == 2   # paired + unpaired branches
    assert 'LATEST_EA_VERSION = "1.63"' in _read("frontend", "src", "components", "EaVersionStrip.jsx")


# ── P3 · one canonical host ─────────────────────────────────────────────────────────────────────────
def test_p3_canonical_host_everywhere():
    mq5 = _read("backend", "static", "EmergentTradingBridge.mq5")
    assert '#define SERVER_URL_DEFAULT "https://stoicaibot.com"' in mq5
    assert 'input string ServerUrl              = "https://stoicaibot.com"' in mq5
    assert "www.stoicaibot.com" not in mq5
    assert "www.stoicaibot.com" not in _read("backend", "static", "STOIC-Installer.ps1")
    assert 'default="https://stoicaibot.com"' in _read("scripts", "live_probe.py")
    assert "-ServerUrl https://stoicaibot.com" in _read("scripts", "spike_webrequest_startup.ps1")
    # the watchdog still ACCEPTS both hosts (www redirects to the apex at the edge)
    import deploy_watch as dw
    assert "stoicaibot.com" in dw.DEFAULT_ALLOWED_HOSTS and "www.stoicaibot.com" in dw.DEFAULT_ALLOWED_HOSTS
