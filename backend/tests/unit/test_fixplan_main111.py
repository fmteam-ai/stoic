"""STOIC main111 review — corrections N111-1 … N111-6.

N111-1  Ops alert list: naive BSON datetimes vs aware now → 500 after an ack/mute; ISO output carries a timezone
N111-2  claim-pairing: terminal_login compared with the account BEFORE consume/rotate → 409, nothing changed
N111-3  installer login detection matches MT5's real "authorized on" journal wording; scans 7 logs
N111-4  install_progress.webrequest_url uses the single resolver (connect_service._base_url)
N111-5  spike: timestamp anchored on HH:mm:ss.fff anywhere; backup before the baseline; exact 4014 text
N111-6  ea_latest_version derived from the MQ5; probe trims pins; https checks consistent (case-sensitive);
        active chart profile only; MUTE hidden on unsupported kinds; mute/unmute audited
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8-sig").read()


# ── N111-1 ────────────────────────────────────────────────────────────────────────────────────────
class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _Coll:
    def __init__(self, docs):
        self.docs = docs

    def find(self, *a, **k):
        return _Cursor(list(self.docs))

    async def count_documents(self, *a, **k):
        return len(self.docs)


def test_n111_1_list_alerts_survives_naive_bson_datetimes(monkeypatch):
    from routes import ops_routes
    now_naive = datetime.utcnow()                                   # what PyMongo hands back (tz_aware=False)
    row = {"_id": "1", "kind": "ea_heartbeat_stale", "severity": "critical", "message": "m", "acked_at": None,
           "created_at": "2026-10-08T00:00:00+00:00", "notify_muted_until": now_naive + timedelta(hours=5)}
    db = type("DB", (), {})()
    db.ops_alerts = _Coll([row])
    db.ops_alert_mutes = _Coll([{"kind": "ea_heartbeat_stale", "muted_until": now_naive + timedelta(hours=20), "muted_by": "admin@x"}])
    monkeypatch.setattr(ops_routes, "get_db", lambda: db)

    async def ok(_request):
        return True, "admin@x"
    monkeypatch.setattr(ops_routes, "_ops_actor", ok)
    res = asyncio.new_event_loop().run_until_complete(ops_routes.list_alerts(request=None))
    assert isinstance(res, dict), getattr(res, "body", res)
    a = res["alerts"][0]
    assert a["snoozed_until"].endswith("+00:00") and a["notify_muted_until"].endswith("+00:00")
    assert datetime.fromisoformat(a["snoozed_until"]) > datetime.now(timezone.utc) + timedelta(hours=19)   # the longer mute wins
    assert res["mutes"][0]["muted_until"].endswith("+00:00")
    assert "ea_heartbeat_stale" in res["mutable_kinds"] and "pairing_no_heartbeat" in res["mutable_kinds"]


def test_n111_1_expired_naive_snooze_is_null(monkeypatch):
    from routes import ops_routes
    row = {"_id": "1", "kind": "policy_expired", "acked_at": None, "notify_muted_until": datetime.utcnow() - timedelta(minutes=1)}
    db = type("DB", (), {})()
    db.ops_alerts = _Coll([row])
    db.ops_alert_mutes = _Coll([])
    monkeypatch.setattr(ops_routes, "get_db", lambda: db)

    async def ok(_request):
        return True, "admin@x"
    monkeypatch.setattr(ops_routes, "_ops_actor", ok)
    res = asyncio.new_event_loop().run_until_complete(ops_routes.list_alerts(request=None))
    assert res["alerts"][0]["snoozed_until"] is None


# ── N111-2 ────────────────────────────────────────────────────────────────────────────────────────
def test_n111_2_claim_rejects_wrong_terminal_before_consuming():
    src = _read("backend", "routes", "setup_routes.py")
    assert "terminal_login: str | None = Field(default=None, max_length=32)" in src
    i_check = src.index('"code": "account_mismatch"')
    i_consume = src.index("claimed = await db.pairing_tokens.find_one_and_update")
    assert i_check < i_consume                                                   # 409 BEFORE the atomic consume
    assert "status_code=409" in src[i_check - 400:i_check]
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    body = ps[ps.index("function Install-Stoic"):]
    assert "terminal_login    = $(if ($terminalLogin)" in body
    assert '$code -eq "account_mismatch"' in body and "the code was not used" in body
    # no post-claim abort remains: the https fallback warns and continues, the login echo only warns
    post = body[body.index("$bridgeToken    = $claimResp.bridge_token"):body.index("[3/5] Downloading")]
    assert "return" not in post.replace("return $", "")                          # (no bare `return` in the post-claim window)
    assert "using $ServerUrl instead" in post


# ── N111-3 ────────────────────────────────────────────────────────────────────────────────────────
def test_n111_3_login_regex_matches_real_mt5_journal_line():
    import re
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    fn = ps[ps.index("function Get-StoicTerminalLogin"):ps.index("function Get-StoicChartSymbol")]
    m = re.search(r'\[regex\]::Matches\(\$text, "(.+?)"\)', fn)
    pattern = m.group(1)
    real = "NS\t0\t10:50:25.187\tNetwork\t'3047910': authorized on Exness-MT5Real through Access Point #2 (ping: 303.47 ms)"
    mt4 = "0\t10:50:25.187\tNetwork\t'12345678': login on Broker-Demo through Access Point EU 1"
    assert re.search(pattern, real).group(1) == "3047910"
    assert re.search(pattern, mt4).group(1) == "12345678"
    assert "Select-Object -First 7" in fn
    t = _read("scripts", "test_installer.ps1")
    assert "authorized on Broker-Demo through Access Point EU 1 (ping: 46.19 ms)" in t


# ── N111-4 ────────────────────────────────────────────────────────────────────────────────────────
def test_n111_4_single_url_resolver(monkeypatch):
    import install_progress as ip
    from connect_service import _base_url
    for k in ("PUBLIC_BASE_URL", "PUBLIC_BACKEND_URL", "REACT_APP_BACKEND_URL", "CORS_ORIGINS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PUBLIC_BACKEND_URL", "https://api.example")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://www.example")
    assert ip.webrequest_url("http://x/", "https", "other") == _base_url(None) == "https://www.example"
    src = _read("backend", "install_progress.py")
    assert 'os.environ.get("PUBLIC_BACKEND_URL")' not in src.split("def webrequest_url")[1].split("\ndef ")[0]


# ── N111-5 ────────────────────────────────────────────────────────────────────────────────────────
def test_n111_5_spike_timestamp_pattern_and_backup_order():
    import re
    sp = _read("scripts", "spike_webrequest_startup.ps1")
    m = re.search(r"\[regex\]::Match\(\$line, '(.+?)'\)", sp)
    pat = m.group(1)
    line = "CS\t0\t10:15:02.123\tExperts\tSTOIC Bridge EA v1.62 started"
    assert re.search(pat, line).groups() == ("10", "15", "02", "123")
    assert "'WebRequest error 4014|4014'" not in sp and "-match 'WebRequest error 4014'" in sp
    assert sp.index("Copy-Item $cfg $backup -Recurse -Force") < sp.index("0 baseline")


# ── N111-6 ────────────────────────────────────────────────────────────────────────────────────────
def test_n111_6_latest_version_is_derived(monkeypatch, tmp_path):
    import ea_capabilities as ec
    assert ec.latest_ea_version() == "1.62"
    assert '"ea_latest_version": latest_ea_version()' in _read("backend", "routes", "setup_routes.py")
    assert 'LATEST_EA = "' not in _read("backend", "routes", "bot_routes.py")
    assert 'LATEST_EA = "' not in _read("backend", "routes", "diagnostic_routes.py")
    fake = tmp_path / "e.mq5"
    fake.write_text('#property version   "9.99"\n')
    monkeypatch.setattr(ec, "_MQ5_PATH", str(fake))
    monkeypatch.setattr(ec, "_latest_cache", {})
    assert ec.latest_ea_version() == "9.99"


def test_n111_6_probe_trims_and_workflow_text():
    pr = _read("scripts", "signer_probe.py")
    assert 'a.public_key = (a.public_key or "").strip()' in pr
    wf = _read(".github", "workflows", "ea-release.yml")
    assert "EA compiled with 0 errors, but" not in wf and "Nothing was compiled" in wf


def test_n111_6_https_checks_consistent_and_active_profile_only():
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert "-cnotmatch '^https://'" in ps and "-notmatch '^https://'" not in ps.replace("-cnotmatch", "")
    fn = ps[ps.index("function Get-StoicChartSymbol"):ps.index("function Resolve-StoicTerminal")]
    assert "ProfileLast=" in fn and 'config\\terminal.ini' in fn and '$profile = "Default"' in fn
    t = _read("scripts", "test_installer.ps1")
    assert "ProfileLast=Scalping" in t


def test_n111_6_mute_hidden_on_unsupported_kinds_and_audited():
    card = _read("frontend", "src", "components", "AlertsCard.jsx")
    assert "mutable_kinds" in card and "canMute" in card and ") : canMute ? (" in card
    src = _read("backend", "routes", "ops_routes.py")
    assert '"ops_alert_mute"' in src and '"ops_alert_unmute"' in src and src.count("from step_up import audit_event") >= 2
