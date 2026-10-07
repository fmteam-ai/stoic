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
    assert '#define EA_CLIENT_VERSION "1.61"' in mq5 and '#property version   "1.61"' in mq5
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
    assert '"ea_latest_version": "1.61"' in _read("backend", "routes", "setup_routes.py")
    assert 'LATEST_EA = "1.61"' in _read("backend", "routes", "bot_routes.py")
    assert 'LATEST_EA_VERSION = "1.61"' in _read("frontend", "src", "components", "EaVersionStrip.jsx")
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
    s = hb({**base, "ea_self_check": {"autotrading": False, "ea_trade_allowed": True, "webrequest_ok": True}})
    assert s["status"] == "warn" and "AutoTrading" in s["hint"]
    s = hb({**base, "ea_self_check": {"autotrading": True, "ea_trade_allowed": False, "webrequest_ok": True}})
    assert s["status"] == "warn" and "Allow Algo Trading" in s["hint"]
    s = hb({**base, "ea_self_check": {"autotrading": True, "ea_trade_allowed": True, "webrequest_ok": True}})
    assert s["status"] == "done"
    s = hb({**base, "last_heartbeat": None, "installer_paired_at": (now - timedelta(minutes=12)).isoformat()})
    assert s["status"] == "blocked" and "inputs at default" in s["hint"]


def test_installer_v1_5_zero_touch_files_and_restart():
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert '$InstallerVersion = "1.5"' in ps
    assert "SecurityProtocolType]::Tls12" in ps
    assert "function Get-StoicRunningTerminal" in ps and "Get-Process terminal64" in ps
    assert "STOIC-Server.txt" in ps and '"ServerUrl=$ServerUrl`r`n"' in ps and "stoic.set" in ps
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
