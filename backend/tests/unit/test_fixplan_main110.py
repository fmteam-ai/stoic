"""STOIC main110 review — corrections N110-2 … N110-10 and N109-1/N109-2.

N110-2  preset carries no ServerUrl; EA https-only on BOTH paths; installer refuses non-https; one URL source
N110-3  restart opt-in (default N) with a warning; no AllowDllImport; no force-kill; suffixed EURUSD detection
N110-4  running terminal must be confirmed (path + login); login mismatch with the pairing code aborts
N110-5  TLS 1.2 before the one-liner's first download
N110-6  spike: lines after $Since only, FAIL baseline required, throwaway portable copy, auto-restore
N110-7  /api/ea-script.ex5 carries X-STOIC-EA-Version; installer compares with ea_latest_version
N110-8  self-check flags age out; unreadable server file → default URL
N110-9  wizard account-type picker
N110-10 drift guard: record NEWER than source fails; DEMO_ACCEPTED_EA comment current
N109-1/2 ack / mute silence NOTIFICATIONS only — the alert row always re-opens; "snoozed until" chip
"""
import asyncio
import hashlib
import json
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


# ── N110-2 ────────────────────────────────────────────────────────────────────────────────────────
def test_n110_2_one_url_source_https_only():
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    body = ps[ps.index("function Install-Stoic"):]
    assert "ServerUrl=$" not in ps                                             # preset has no override
    assert "notmatch '^https://'" in body and "must start with https://" in body
    assert "$eaServerUrl    = if ($claimResp.server_url)" in body               # drop file from the claim response
    assert body.index("$eaServerUrl") < body.index("STOIC-Server.txt")
    mq5 = _read("backend", "static", "EmergentTradingBridge.mq5")
    fn = mq5[mq5.index("string ResolveServerUrl()"):mq5.index("int OnInit()")]
    assert 'StringFind(input_trim, "https://") == 0' in fn                     # input path checked too
    assert "ignoring ServerUrl input (must start with https://)" in fn
    assert "return input_trim;" in fn and fn.count("return input_trim") == 1   # only after the https check
    sr = _read("backend", "routes", "setup_routes.py")
    assert "from connect_service import _base_url" in sr and "backend_base = (_base_url(request)" in sr
    assert 'os.environ.get(\n        "PUBLIC_BACKEND_URL"' not in sr


def test_n110_2_base_url_single_resolver(monkeypatch):
    import connect_service as cs
    for k in ("PUBLIC_BASE_URL", "CORS_ORIGINS", "PUBLIC_BACKEND_URL", "REACT_APP_BACKEND_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PUBLIC_BACKEND_URL", "https://api.example/")
    assert cs._base_url(None) == "https://api.example"
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://www.example")
    assert cs._base_url(None) == "https://www.example"                         # PUBLIC_BASE_URL still wins


# ── N110-3 / N110-4 ───────────────────────────────────────────────────────────────────────────────
def test_n110_3_restart_is_opt_in_and_gentle():
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert "AllowDllImport" not in ps and "Stop-Process" not in ps
    assert "Restart MetaTrader 5 now? [y/N]" in ps and "UNMANAGED" in ps and "every other EA" in ps
    assert "if ($answer -match '^[Yy]')" in ps                                  # Enter = No
    assert "function Get-StoicChartSymbol" in ps and "profiles\\charts" in ps and "Symbol=$chartSymbol" in ps
    assert "WaitForExit(60000)" in ps


def test_n110_4_terminal_confirmation_and_login_guard():
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert "function Get-StoicTerminalLogin" in ps and "login on" in ps
    res = ps[ps.index("function Resolve-StoicTerminal"):ps.index("function Test-StoicCompileLog")]
    assert "[scriptblock]$Confirm" in res and "Is this the terminal logged into the account this pairing code belongs to? [y/N]" in res
    body = ps[ps.index("function Install-Stoic"):]
    assert "$terminalLogin = Get-StoicTerminalLogin" in body
    # N111-2 — the mismatch is now refused by the SERVER before the claim consumes the code (see test_fixplan_main111)
    assert "terminal_login" in body and "account_mismatch" in body
    t = _read("scripts", "test_installer.ps1")
    assert "running terminal NOT confirmed" in t and "Get-StoicChartSymbol" in t and "Get-StoicTerminalLogin" in t


# ── N110-5 ────────────────────────────────────────────────────────────────────────────────────────
def test_n110_5_tls12_prefix():
    import connect_service as cs
    cmd = cs.install_command("https://s.example", "t", "A" * 64)
    assert cmd.startswith("[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $r=iwr")


# ── N110-6 ────────────────────────────────────────────────────────────────────────────────────────
def test_n110_6_spike_is_safe_and_measures_only_new_lines():
    sp = _read("scripts", "spike_webrequest_startup.ps1")
    assert "[Parameter(Mandatory = $true)][string]$PortableCopy" in sp and "-DataFolder" not in sp.split("param(")[1].split(")")[0]
    assert "refusing: $DataFolder is an installed terminal" in sp
    assert "function Get-LinesSince" in sp and "if ($ts -ge $Since)" in sp and "Select-Object -Last 400" not in sp
    assert "baseline: NO 4014 error" in sp and sp.index("0 baseline") < sp.index("A startup-ini keys")
    assert "finally {" in sp and "Copy-Item $backup $cfg -Recurse -Force" in sp
    assert "Stop-Process" not in sp
    doc = _read("docs", "EASY_CONNECT_SPIKE.md")
    assert "-PortableCopy" in doc and "THROWAWAY" in doc


# ── N110-7 ────────────────────────────────────────────────────────────────────────────────────────
def test_n110_7_ex5_version_header(tmp_path, monkeypatch):
    import server
    src = _read("backend", "server.py")
    assert '"X-STOIC-EA-Version": _ex5_version_for(digest)' in src
    rel = tmp_path / "ea_release.json"
    rel.write_text(json.dumps({"version": "1.61", "ex5_sha256": "a" * 64, "previous": {"version": "1.60", "ex5_sha256": "b" * 64}}))
    import ea_capabilities
    monkeypatch.setattr(ea_capabilities, "_EA_RELEASE_FILES", (str(rel),))
    assert server._ex5_version_for("A" * 64) == "1.61"
    assert server._ex5_version_for("b" * 64) == "1.60"
    assert server._ex5_version_for("c" * 64) == "unknown"
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert "X-STOIC-EA-Version" in ps and '$ex5Version -ne "$eaLatestVer"' in ps and "not deploying it" in ps


# ── N110-8 ────────────────────────────────────────────────────────────────────────────────────────
def test_n110_8_self_check_ageing_and_default_fallback():
    import install_progress as ip
    assert ip.SELF_CHECK_MAX_AGE_S == 15 * 60
    mq5 = _read("backend", "static", "EmergentTradingBridge.mq5")
    fn = mq5[mq5.index("string ResolveServerUrl()"):mq5.index("int OnInit()")]
    assert "if (fh == INVALID_HANDLE) return input_trim;" not in fn
    assert fn.count("return SERVER_URL_DEFAULT;") >= 3                         # no file / unreadable / non-https
    assert '"1.62"' in mq5 and '#define EA_CLIENT_VERSION "1.62"' in mq5


# ── N110-9 ────────────────────────────────────────────────────────────────────────────────────────
def test_n110_9_wizard_account_type_picker():
    wz = _read("frontend", "src", "components", "AddAccountWizard.jsx")
    assert "const ACCOUNT_TYPES" in wz
    for t in ("standard", "cent", "microcent", "demo"):
        assert f'id: "{t}"' in wz
    assert 'data-testid={`wizard-account-type-${t.id}`}' in wz and 'set("account_type", t.id)' in wz
    assert "you'll run the install line" in wz
    panel = _read("frontend", "src", "components", "QuickInstallPanel.jsx")
    assert "default <strong>N</strong>" in panel and "unmanaged" in panel


# ── N110-10 ───────────────────────────────────────────────────────────────────────────────────────
def test_n110_10_drift_guard_version_order(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("drift", os.path.join(ROOT, "scripts", "check_release_hash_drift.py"))
    drift = importlib.util.module_from_spec(spec); spec.loader.exec_module(drift)
    mq5 = tmp_path / "e.mq5"
    mq5.write_text('#property version   "1.61"\n', encoding="utf-8")
    sha = hashlib.sha256(mq5.read_bytes()).hexdigest()
    hashes = tmp_path / "h.json"
    hashes.write_text(json.dumps({"ea": {"version": "1.61", "mq5_sha256": sha}}))
    rel = tmp_path / "r.json"
    rel.write_text(json.dumps({"version": "1.60", "mq5_sha256": "0" * 64, "ex5_sha256": "1" * 64}))
    assert drift.check(str(mq5), str(hashes), str(rel)) == []                   # older record → pending re-release
    rel.write_text(json.dumps({"version": "1.62", "mq5_sha256": "0" * 64, "ex5_sha256": "1" * 64}))
    fails = drift.check(str(mq5), str(hashes), str(rel))
    assert len(fails) == 1 and "OLDER" in fails[0]                             # rollback → FAIL
    rel.write_text(json.dumps({"version": "1.9", "mq5_sha256": "0" * 64, "ex5_sha256": "1" * 64}))
    assert drift.check(str(mq5), str(hashes), str(rel)) == []                   # numeric, not lexicographic (1.9 < 1.61)
    dr = _read("backend", "demo_readiness.py")
    assert "until a signed 1.60 ships" not in dr and "N110-10" in dr


# ── N109-1 / N109-2 ───────────────────────────────────────────────────────────────────────────────
class _Coll:
    def __init__(self, acked=None, mute=None):
        self.acked, self.mute, self.rows = acked, mute, []

    async def find_one(self, flt, sort=None, projection=None):
        if "muted_until" in str(flt) or flt == {"kind": flt.get("kind")} and "acked_at" not in flt:
            return self.mute
        if flt.get("acked_at") is None:
            return None
        if self.acked and re.match(flt["acked_by"]["$not"]["$regex"], self.acked["acked_by"]):
            return None
        return self.acked

    async def insert_one(self, doc):
        self.rows.append(doc)
        return type("R", (), {"inserted_id": "x"})()

    async def update_one(self, *a, **k):
        return None


def _db(acked=None, mute=None):
    db = type("DB", (), {})()
    db.ops_alerts = _Coll(acked=acked)
    db.ops_alert_mutes = _Coll(mute=mute)
    return db


def test_n109_1_ack_mutes_notifications_never_the_row(monkeypatch):
    import alerting
    import guard_alerts
    sent = []
    monkeypatch.setattr(guard_alerts, "queue_ops_alert_email", lambda *a, **k: sent.append(a))
    now = datetime.now(timezone.utc)
    db = _db(acked={"acked_by": "admin@x", "acked_at": now - timedelta(hours=1)})
    rid = asyncio.new_event_loop().run_until_complete(
        alerting.raise_alert(db, "ea_heartbeat_stale", "critical", "m", dedup_key="ea_heartbeat:1"))
    assert rid is not None and len(db.ops_alerts.rows) == 1                     # row re-opened → gates see it
    row = db.ops_alerts.rows[0]
    assert row["acked_at"] is None and row["notify_muted_until"] is not None
    assert abs((row["notify_muted_until"] - (now + timedelta(hours=5))).total_seconds()) < 5
    assert sent == []                                                           # push muted
    db = _db(acked={"acked_by": "admin@x", "acked_at": now - timedelta(hours=7)})
    asyncio.new_event_loop().run_until_complete(
        alerting.raise_alert(db, "ea_heartbeat_stale", "critical", "m", dedup_key="ea_heartbeat:1"))
    assert db.ops_alerts.rows[0]["notify_muted_until"] is None and len(sent) == 1   # window over → notified


def test_n109_2_explicit_kind_mute_and_api(monkeypatch):
    import alerting
    now = datetime.now(timezone.utc)
    until = asyncio.new_event_loop().run_until_complete(
        alerting.notification_mute_until(_db(mute={"kind": "pairing_no_heartbeat", "muted_until": now + timedelta(hours=20)}),
                                         "pairing_no_heartbeat", "pairing:a", now))
    assert until is not None and abs((until - (now + timedelta(hours=20))).total_seconds()) < 2
    expired = asyncio.new_event_loop().run_until_complete(
        alerting.notification_mute_until(_db(mute={"kind": "pairing_no_heartbeat", "muted_until": now - timedelta(minutes=1)}),
                                         "pairing_no_heartbeat", "pairing:a", now))
    assert expired is None
    assert alerting.MUTE_MAX_H == 24
    src = _read("backend", "routes", "ops_routes.py")
    assert '@router.post("/ops/alerts/mute")' in src and '@router.delete("/ops/alerts/mute/{kind}")' in src
    assert 'actor == "metrics-token"' in src and 'a["snoozed_until"]' in src and '"mutes": [' in src
    pa = _read("backend", "pairing_alerts.py")
    assert "notification_mute_until(db, KIND, key, now)" in pa and "if not synthetic and not muted:" in pa
    card = _read("frontend", "src", "components", "AlertsCard.jsx")
    assert "NOTIFICATIONS SNOOZED UNTIL" in card and "MUTE 24H" in card and '"/ops/alerts/mute"' in card
    assert 'data-testid={`ops-alert-snoozed-${a.kind}`}' in card
