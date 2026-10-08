"""Easy MT5 Connect — Phase 1 (EA 1.61 + installer v1.5 + 60-min code): the user never edits EA inputs.
EA: ServerUrl auto-loaded from MQL5\\Files\\STOIC-Server.txt (preview default gone), startup self-check
flags in every heartbeat; server stores them; Install Progress turns them into the exact fix.
Installer: picks the RUNNING terminal, writes STOIC-Server.txt + stoic.set + startup config, TLS 1.2,
offers an MT5 restart that attaches the EA itself."""
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


def test_ea_1_61_server_url_autoload_and_self_check():
    mq5 = _read("backend", "static", "EmergentTradingBridge.mq5")
    assert '#define EA_CLIENT_VERSION "1.62"' in mq5 and '#property version   "1.62"' in mq5
    assert "preview.emergentagent.com" not in mq5.split("string ResolveServerUrl")[0].split("input string ServerUrl")[1].split("\n")[0]
    assert 'input string ServerUrl              = "https://www.stoicaibot.com"' in mq5
    assert 'FileIsExist("STOIC-Server.txt")' in mq5 and "g_server_url = ResolveServerUrl();" in mq5
    assert 'ServerUrl + "' not in mq5                      # every request goes through the resolved URL
    assert mq5.count('g_server_url + "/api/bridge/') >= 20
    assert '\\"ea_self_check\\":{\\"autotrading\\":%s,\\"ea_trade_allowed\\":%s,\\"webrequest_ok\\":%s}' in mq5
    assert "TERMINAL_TRADE_ALLOWED" in mq5 and "MQL_TRADE_ALLOWED" in mq5
    assert "WebRequest error 4014 — FIX:" in mq5
    # the resolve happens BEFORE the first heartbeat
    init = mq5[mq5.index("int OnInit()"):]
    assert init.index("g_server_url = ResolveServerUrl();") < init.index("SendHeartbeat();")
    # version surfaces
    assert '"ea_latest_version": "1.62"' in _read("backend", "routes", "setup_routes.py")
    assert 'LATEST_EA = "1.62"' in _read("backend", "routes", "bot_routes.py")
    assert 'LATEST_EA_VERSION = "1.62"' in _read("frontend", "src", "components", "EaVersionStrip.jsx")
    import demo_readiness as dr
    assert "1.60" in dr.DEMO_ACCEPTED_EA                   # a 1.60 terminal stays accepted during the demo


def test_heartbeat_stores_bool_self_check_flags_only():
    import models
    assert "ea_self_check" in models.BridgeHeartbeat.model_fields
    src = _read("backend", "routes", "bridge_routes.py")
    assert 'set_doc["ea_self_check"] = {**flags, "at": now_iso}' in src
    assert 'isinstance(sc.get(k), bool)' in src


def test_install_progress_turns_flags_into_the_exact_fix():
    import install_progress as ip
    now = datetime.now(timezone.utc)
    base = {"_id": "a1", "installer_paired_at": (now - timedelta(minutes=5)).isoformat(),
            "installer_paired_hostname": "VPS", "installer_version": "1.5",
            "last_heartbeat": (now - timedelta(seconds=10)).isoformat(), "ea_version": "1.61"}
    def hb(acc):
        res = ip.derive(acc, None, None, attested=("unmeasured", None), accepted_hashes=[], now=now, request_base="https://s.example/")
        return next(s for s in res["steps"] if s["id"] == "heartbeat")
    fresh = now.isoformat()
    s = hb({**base, "ea_self_check": {"autotrading": False, "ea_trade_allowed": True, "webrequest_ok": True, "at": fresh}})
    assert s["status"] == "warn" and "AutoTrading" in s["hint"]
    s = hb({**base, "ea_self_check": {"autotrading": True, "ea_trade_allowed": False, "webrequest_ok": True, "at": fresh}})
    assert s["status"] == "warn" and "Allow Algo Trading" in s["hint"]
    s = hb({**base, "ea_self_check": {"autotrading": True, "ea_trade_allowed": True, "webrequest_ok": True, "at": fresh}})
    assert s["status"] == "done"
    # N110-8 — a stale (or undated) autotrading:false hint no longer sticks
    s = hb({**base, "ea_self_check": {"autotrading": False, "ea_trade_allowed": True, "at": (now - timedelta(minutes=16)).isoformat()}})
    assert s["status"] == "done"
    s = hb({**base, "ea_self_check": {"autotrading": False, "ea_trade_allowed": True}})
    assert s["status"] == "done"
    s = hb({**base, "last_heartbeat": None, "installer_paired_at": (now - timedelta(minutes=12)).isoformat()})
    assert s["status"] == "blocked" and "inputs at default" in s["hint"]


def test_installer_v1_5_zero_touch_files_and_restart():
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert '$InstallerVersion = "1.6"' in ps
    assert "SecurityProtocolType]::Tls12" in ps
    assert "function Get-StoicRunningTerminal" in ps and "Get-Process terminal64" in ps
    assert "STOIC-Server.txt" in ps and "ServerUrl=$" not in ps and "stoic.set" in ps     # N110-2 — preset carries no ServerUrl
    ini = ps[ps.index("[Experts]"):ps.index("Period=$ChartPeriod")]
    assert "AllowLiveTrading=1" in ini and "Expert=EmergentTradingBridge" in ini and "ExpertParameters=stoic.set" in ini
    assert "function Restart-StoicTerminal" in ps and "/config:" in ps and "CloseMainWindow()" in ps
    assert "[switch]$NoRestart" in ps and '[string]$ChartSymbol = "EURUSD"' in ps
    # the running terminal is tried before the interactive prompt
    body = ps[ps.index("function Resolve-StoicTerminal"):]
    assert body.index("Get-StoicRunningTerminal -Candidates $found") < body.index("Choose the terminal for it")
    # files are written BEFORE the restart, restart BEFORE the summary
    assert ps.index("stoic-start.ini") < ps.index("Restart-StoicTerminal -Exe") < ps.index("[5/5] All set.")
    assert "Do NOT edit the EA inputs" in ps
    t = _read("scripts", "test_installer.ps1")
    assert "running terminal (origin.txt match) auto-selected among several" in t


def test_pairing_code_lasts_an_hour_and_ui_says_new_code():
    import routes.setup_routes as sr
    assert sr.PAIRING_TTL_MINUTES == 60
    panel = _read("frontend", "src", "components", "QuickInstallPanel.jsx")
    assert "NEW CODE" in panel and "REGENERATE TOKEN" not in panel
    assert "Nothing to type in the EA inputs" in panel
    assert re.search(r"ServerUrl set to this server", _read("backend", "install_progress.py")) is None


def test_pairing_silent_alert_also_reaches_the_account_owner():
    import asyncio
    from datetime import datetime, timedelta, timezone
    import pairing_alerts as pa
    now = datetime.now(timezone.utc)
    acc = {"_id": "a1", "label": "OnEquity", "user_id": "u1", "installer_paired_at": (now - timedelta(minutes=15)).isoformat(),
           "installer_paired_hostname": "VPS-1", "installer_version": "1.5", "last_heartbeat": None}
    meta = {"account_id": "a1", "host": "VPS-1", "silent_s": 900, "never_heartbeated": True, "webrequest_url": "https://s.example"}
    lines = pa.owner_lines(acc, meta, "https://s.example")
    assert "15 min" in lines[0] and "https://s.example" in lines[2] and any("AutoTrading" in ln for ln in lines)

    class _Cur:
        def __init__(self, docs): self.docs = docs
        def sort(self, *a): return self
        def limit(self, *a): return self
        def __aiter__(self):
            async def g():
                for d in self.docs:
                    yield d
            return g()

    class _Coll:
        def __init__(self, docs): self.docs = docs
        def find(self, *a, **k): return _Cur(self.docs)

    db = type("DB", (), {})()
    db.accounts, db.ops_alerts = _Coll([acc]), _Coll([])
    calls = {"ops": [], "owner": []}

    async def raise_alert(_db, *a, **k): return "id"
    async def notify(text): calls["ops"].append(text)
    async def owner(_db, a, m, url): calls["owner"].append((a["_id"], m["silent_s"], url))
    asyncio.new_event_loop().run_until_complete(pa.evaluate(db, now, raise_alert=raise_alert, notify=notify, owner_notify=owner))
    assert len(calls["ops"]) == 1 and calls["owner"] == [("a1", 900, calls["owner"][0][2])]


def test_audit10_owner_email_is_html_escaped_and_ea_requires_https_drop_file():
    import pairing_alerts as pa
    acc = {"_id": "a1", "label": "<b>x</b>", "user_id": "u1"}
    meta = {"host": "<img src=x onerror=alert(1)>", "silent_s": 600, "never_heartbeated": True}
    lines = pa.owner_lines(acc, meta, "https://s.example")
    from html import escape
    assert "<img" in lines[0] and "<img" not in escape(lines[0])
    assert "escape(ln) for ln in lines" in _read("backend", "pairing_alerts.py")
    mq5 = _read("backend", "static", "EmergentTradingBridge.mq5")
    assert 'StringFind(url, "https://") != 0' in mq5 and 'StringFind(url, "http") != 0' not in mq5


def test_installer_hash_pin_in_one_liner_and_endpoint():
    import hashlib
    import connect_service as cs
    raw = open(os.path.join(ROOT, "backend", "static", "STOIC-Installer.ps1"), "rb").read()
    sha = hashlib.sha256(raw).hexdigest().upper()
    assert cs.installer_sha256() == sha and len(sha) == 64
    cmd = cs.install_command("https://s.example", "tok123")
    assert cmd.startswith('[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; '
                          '$r=iwr "https://s.example/api/setup/installer.ps1" -UseBasicParsing; ')    # N110-5 — TLS 1.2 before the first download
    assert f'if($h -ne "{sha}")' in cmd and "throw" in cmd and "Get-FileHash -InputStream $r.RawContentStream" in cmd
    assert cmd.index("throw") < cmd.index("iex (")                      # verify BEFORE execute
    assert cmd.endswith('Install-Stoic -Token "tok123" -ServerUrl "https://s.example"')
    assert "TrimStart([char]0xFEFF)" in cmd                             # served file carries a UTF-8 BOM
    assert '@api_router.get("/setup/installer.sha256")' in _read("backend", "server.py")
    sr = _read("backend", "routes", "setup_routes.py")
    assert '"installer_sha256": sha' in sr and '"install_command": install_command(base, token, sha)' in sr
    panel = _read("frontend", "src", "components", "QuickInstallPanel.jsx")
    assert "pin?.install_command" in panel and "quick-install-hash-pin" in panel


def test_add_account_wizard_wired_and_spec_brokers_present():
    from broker_presets import BROKER_PRESETS
    names = {p["broker"] for p in BROKER_PRESETS}
    for b in ("IC Markets", "Pepperstone", "Vantage", "RoboForex", "VT Markets", "STARTRADER", "OnEquity", "Tauro Markets"):
        assert b in names, b
    assert all(p["servers"] and p["account_types"] for p in BROKER_PRESETS)
    wiz = _read("frontend", "src", "components", "AddAccountWizard.jsx")
    for tid in ("wizard-broker-select", "wizard-server-select", "wizard-login-input", "wizard-vps-new", "wizard-step-review", "wizard-create-btn", "wizard-done"):
        assert f'"{tid}"' in wiz or f"'{tid}'" in wiz or f"`{tid}`" in wiz or tid in wiz, tid
    assert 'api.post("/accounts", payload)' in wiz and "<QuickInstallPanel" in wiz
    acc = _read("frontend", "src", "pages", "Accounts.jsx")
    assert "<AddAccountWizard" in acc and 'onClick={() => setShowWizard(true)} data-testid="add-account-button"' in acc
    assert "installer_paired_hostname" in acc           # VPS picker lists already-paired hosts


def test_forexvps_referral_is_the_recommended_vps_everywhere():
    import integrations_settings as integ
    from unittest.mock import patch
    assert integ.REGISTRY["VPS_REFERRAL_URL"][0] == "vps" and not integ.REGISTRY["VPS_REFERRAL_URL"][1]
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("VPS_REFERRAL_URL", None)
        o = integ.vps_offer()
        assert o["url"] == "https://www.forexvps.net/partner/stoicaibot" and o["provider"] == "ForexVPS"
        os.environ["VPS_REFERRAL_URL"] = "http://evil.example"          # non-https override is ignored
        assert integ.vps_offer()["url"] == integ.DEFAULT_VPS_REFERRAL_URL
        os.environ["VPS_REFERRAL_URL"] = "https://www.forexvps.net/partner/other"
        assert integ.vps_offer()["url"].endswith("/partner/other")
    assert '@api_router.get("/public/vps-offer")' in _read("backend", "server.py")
    for rel, tid in (("frontend/src/components/AddAccountWizard.jsx", "wizard-vps-offer-link"),
                     ("frontend/src/pages/Accounts.jsx", "conn-vps-offer-link"),
                     ("frontend/src/pages/Guide.jsx", "guide-vps-offer-link"),
                     ("frontend/src/components/QuickInstallPanel.jsx", "quick-install-vps-offer-link")):
        assert tid in _read(*rel.split("/")), rel
    vo = _read("frontend", "src", "components", "VpsOffer.jsx")
    assert 'api.get("/public/vps-offer")' in vo and 'rel="noopener noreferrer sponsored"' in vo


def test_release_hash_drift_guard_allows_version_bump_but_blocks_silent_drift(tmp_path):
    import hashlib
    import importlib.util
    import json
    spec = importlib.util.spec_from_file_location("drift", os.path.join(ROOT, "scripts", "check_release_hash_drift.py"))
    drift = importlib.util.module_from_spec(spec); spec.loader.exec_module(drift)
    mq5 = tmp_path / "EmergentTradingBridge.mq5"
    mq5.write_text('#property version   "1.61"\nint OnInit(){return 0;}\n', encoding="utf-8")   # fixture versions, not the shipped EA
    sha = hashlib.sha256(mq5.read_bytes()).hexdigest()
    hashes = tmp_path / "RELEASE_HASHES.json"
    hashes.write_text(json.dumps({"ea": {"version": "1.61", "mq5_sha256": sha}}))
    rel = tmp_path / "ea_release.json"
    # signed record for the OLDER version → pending re-release, not a failure
    rel.write_text(json.dumps({"version": "1.60", "mq5_sha256": "0" * 64, "ex5_sha256": "1" * 64}))
    assert drift.check(str(mq5), str(hashes), str(rel)) == []
    # signed record for the SAME version but a different source → silent drift → fail
    rel.write_text(json.dumps({"version": "1.61", "mq5_sha256": "0" * 64, "ex5_sha256": "1" * 64}))
    fails = drift.check(str(mq5), str(hashes), str(rel))
    assert len(fails) == 1 and "SAME" in fails[0]
