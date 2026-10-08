"""VPS Agent Service (Easy-Connect Phase 2) — per-account portable MT5 terminals managed by STOIC-Agent.ps1."""
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


class _Cursor:
    def __init__(self, docs):
        self.docs = docs

    def sort(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def __aiter__(self):
        self._it = iter(list(self.docs))
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class _Coll:
    def __init__(self, docs=None):
        self.docs = list(docs or [])
        self.updates, self.inserted, self.deleted = [], [], []

    def _match(self, d, flt):
        for k, v in flt.items():
            if k == "$or":
                if not any(self._match(d, o) for o in v):
                    return False
                continue
            cur = d
            for part in k.split("."):
                cur = (cur or {}).get(part) if isinstance(cur, dict) else None
            if isinstance(v, dict):
                if "$ne" in v and cur == v["$ne"]:
                    return False
                if "$exists" in v and (cur is not None) != v["$exists"]:
                    return False
                if "$nin" in v and cur in v["$nin"]:
                    return False
                if "$gt" in v and not (cur is not None and cur > v["$gt"]):
                    return False
            elif cur != v:
                return False
        return True

    def find(self, flt=None, projection=None):
        return _Cursor([d for d in self.docs if self._match(d, flt or {})])

    async def find_one(self, flt, projection=None, sort=None):
        for d in self.docs:
            if self._match(d, flt):
                return d
        return None

    async def find_one_and_update(self, flt, upd, return_document=None):
        d = await self.find_one(flt)
        if d is not None and "$inc" in upd:
            for k, v in upd["$inc"].items():
                d[k] = d.get(k, 0) + v
        return d

    async def count_documents(self, flt):
        return len([d for d in self.docs if self._match(d, flt)])

    async def update_one(self, flt, upd, upsert=False):
        d = await self.find_one(flt)
        if d is None and upsert:
            d = dict(flt); self.docs.append(d)
            for k, v in (upd.get("$setOnInsert") or {}).items():
                d[k] = v
        if d is not None:
            d.update(upd.get("$set") or {})
        self.updates.append((flt, upd))

    async def update_many(self, flt, upd):
        for d in self.docs:
            if self._match(d, flt):
                d.update(upd.get("$set") or {})

    async def insert_one(self, doc):
        self.docs.append(doc); self.inserted.append(doc)
        return type("R", (), {"inserted_id": "x"})()

    async def delete_many(self, flt):
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._match(d, flt)]
        self.deleted.append(before - len(self.docs))


OID = "66f000000000000000000001"
NOW = datetime.now(timezone.utc)


def _db(agent_hb=NOW):
    from bson import ObjectId
    db = type("DB", (), {})()
    db.accounts = _Coll([{"_id": ObjectId(OID), "user_id": "u1", "account_number": "12345678", "server": "Broker-Demo",
                          "broker": "Broker", "mode": "live", "last_heartbeat": (NOW - timedelta(minutes=10)).isoformat()}])
    db.vps_agents = _Coll([{"_id": "a", "agent_id": "agt_1", "user_id": "u1", "deployment_id": "dep_1", "revoked": False,
                            "last_heartbeat": agent_hb, "command_seq": 0, "facts": {"hostname": "VPS-LON"},
                            "last_metrics": {"golden_ready": True}, "registered_at": NOW}])
    db.pairing_tokens = _Coll([{"token": "old", "account_id": OID}])
    db.agent_commands = _Coll([])
    db.mt5_instances = _Coll([])
    db.ops_alerts = _Coll([])
    db.ops_alert_mutes = _Coll([])
    return db


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_list_agents_reports_online_and_golden_state():
    import vps_terminals as vt
    agents = _run(vt.list_agents(_db(), "u1"))
    assert agents[0]["online"] is True and agents[0]["golden_ready"] is True and agents[0]["hostname"] == "VPS-LON"
    assert _run(vt.list_agents(_db(agent_hb=NOW - timedelta(minutes=10)), "u1"))[0]["online"] is False


def test_queue_install_terminal_issues_token_and_signed_command():
    import vps_terminals as vt
    db = _db()
    res = _run(vt.queue_install_terminal(db, {"id": "u1"}, OID, "agt_1", "https://www.example"))
    assert res["ok"] and res["login"] == "12345678" and res["command_id"].startswith("cmd_")
    assert db.pairing_tokens.deleted == [1]                                             # old unused token replaced
    tok = db.pairing_tokens.inserted[0]
    assert tok["issued_for"] == "vps_agent:agt_1" and tok["account_id"] == OID
    cmd = db.agent_commands.docs[0]
    assert cmd["command"] == "install_terminal" and cmd["params"]["pairing_token"] == tok["token"]
    assert cmd["params"]["server_url"] == "https://www.example" and cmd["params"]["login"] == "12345678" and cmd["seq"] == 1
    assert db.accounts.docs[0]["vps_terminal"]["status"] == "queued"


def test_queue_install_refuses_offline_agent_paper_and_foreign_account():
    import vps_terminals as vt
    with pytest.raises(ValueError, match="offline"):
        _run(vt.queue_install_terminal(_db(agent_hb=NOW - timedelta(minutes=10)), {"id": "u1"}, OID, "agt_1", "https://x"))
    db = _db(); db.accounts.docs[0]["mode"] = "paper"
    with pytest.raises(ValueError, match="paper"):
        _run(vt.queue_install_terminal(db, {"id": "u1"}, OID, "agt_1", "https://x"))
    with pytest.raises(LookupError, match="not found"):
        _run(vt.queue_install_terminal(_db(), {"id": "someone-else"}, OID, "agt_1", "https://x"))


def test_terminals_status_marks_stale_heartbeat_for_restart():
    import vps_terminals as vt
    db = _db()
    db.mt5_instances.docs.append({"agent_id": "agt_1", "account_ref": "12345678", "account_id": OID, "directory": "C:\\STOIC\\MT5\\account-12345678\\", "status": "running"})
    st = _run(vt.terminals_status(db, {"agent_id": "agt_1", "user_id": "u1"}))
    t = st["terminals"][0]
    assert t["stale"] is True and t["restart_wanted"] is True and t["heartbeat_age_s"] >= 590
    assert st["stale_after_s"] == 180 and st["max_restarts_per_hour"] == 3
    db.accounts.docs[0]["last_heartbeat"] = NOW.isoformat()
    assert _run(vt.terminals_status(db, {"agent_id": "agt_1", "user_id": "u1"}))["terminals"][0]["restart_wanted"] is False
    db.mt5_instances.docs[0]["status"] = "awaiting_login"; db.accounts.docs[0]["last_heartbeat"] = None
    assert _run(vt.terminals_status(db, {"agent_id": "agt_1", "user_id": "u1"}))["terminals"][0]["restart_wanted"] is False   # never restart a terminal waiting for its login


def test_report_terminal_mirrors_account_and_alerts_on_restart_loop(monkeypatch):
    import vps_terminals as vt
    import alerting
    raised = []

    async def fake_raise(db, kind, sev, msg, dedup_key=None, meta=None, **k):
        raised.append((kind, sev, dedup_key)); return "id"
    monkeypatch.setattr(alerting, "raise_alert", fake_raise)
    db = _db()
    agent = {"agent_id": "agt_1", "user_id": "u1", "deployment_id": "dep_1"}
    _run(vt.report_terminal(db, agent, {"login": "12345678", "account_id": OID, "status": "running", "detail": "started"}))
    inst = db.mt5_instances.docs[0]
    assert inst["status"] == "running" and inst["directory"].endswith("account-12345678\\")
    assert db.accounts.docs[0]["vps_terminal"]["status"] == "running" and raised == []
    _run(vt.report_terminal(db, agent, {"login": "12345678", "account_id": OID, "status": "restart_loop", "restarts_last_hour": 3}))
    assert raised == [("vps_terminal_restart_loop", "critical", "vps_terminal:agt_1:12345678")]
    assert "vps_terminal_restart_loop" in alerting.EVALUATOR_KINDS                           # mutable like the other evaluator kinds
    with pytest.raises(ValueError):
        _run(vt.report_terminal(db, agent, {"login": "12345678", "status": "weird"}))


def test_queue_restart_terminal_requires_managed_terminal_and_online_agent():
    import vps_terminals as vt
    db = _db()
    with pytest.raises(ValueError, match="install it on the VPS first"):
        _run(vt.queue_restart_terminal(db, {"id": "u1"}, OID, "agt_1"))
    db.mt5_instances.docs.append({"agent_id": "agt_1", "account_ref": "12345678", "account_id": OID, "directory": "C:\\STOIC\\MT5\\account-12345678\\", "status": "running"})
    db.accounts.docs[0]["vps_terminal"] = {"status": "running", "agent_id": "agt_1"}
    res = _run(vt.queue_restart_terminal(db, {"id": "u1"}, OID, "agt_1"))
    cmd = db.agent_commands.docs[-1]
    assert res["ok"] and cmd["command"] == "restart_terminal" and cmd["params"]["login"] == "12345678" and cmd["params"]["directory"].endswith("account-12345678\\")
    assert db.accounts.updates[-1][1]["$set"]["vps_terminal.restart_command_id"] == res["command_id"]
    with pytest.raises(ValueError, match="offline"):
        _run(vt.queue_restart_terminal(_db(agent_hb=NOW - timedelta(minutes=10)), {"id": "u1"}, OID, "agt_1"))
    with pytest.raises(LookupError, match="not found"):
        _run(vt.queue_restart_terminal(_db(), {"id": "other"}, OID, "agt_1"))


def test_enrol_one_liner_pins_agent_hash(monkeypatch):
    import vps_pathb as pb
    for k in ("PUBLIC_BASE_URL", "PUBLIC_BACKEND_URL", "REACT_APP_BACKEND_URL", "CORS_ORIGINS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://www.example")
    cmd = pb.vps_agent_enrol_command("ABC-123")
    sha = pb.agent_script_sha256()
    assert len(sha) == 64 and sha.upper() in cmd
    assert cmd.startswith("[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; ")
    assert '$r=iwr "https://www.example/api/setup/agent.ps1" -UseBasicParsing' in cmd
    assert 'Install-StoicAgent -ServerUrl "https://www.example" -EnrollmentCode "ABC-123"' in cmd
    src = _read("backend", "vps_pathb.py")
    assert '"vps_agent_command": agent_cmd' in src and '"vps_agent_sha256": agent_script_sha256()' in src
    wiz = _read("frontend", "src", "components", "InfraWizard.jsx")
    assert "pathbResult.vps_agent_command" in wiz and 'data-testid="vps-agent-enrol-copy"' in wiz
    panel = _read("frontend", "src", "components", "InstallProgressPanel.jsx")
    assert "restart-terminal" in panel and "install-vps-restart-" in panel
    routes = _read("backend", "routes", "vps_terminal_routes.py")
    assert '@router.post("/agents/{agent_id}/restart-terminal")' in routes
    ps = _read("backend", "static", "STOIC-Agent.ps1")
    assert 'dashboard restart: $detail' in ps and "not force-killed" in ps


def test_wiring_routes_commands_and_install_progress():
    from vps_pathb import ALLOWED_COMMANDS
    assert "install_terminal" in ALLOWED_COMMANDS
    src = _read("backend", "routes", "vps_terminal_routes.py")
    for r in ('@router.get("/agents")', '@router.post("/agents/{agent_id}/install-terminal")',
              '@router.post("/agent/terminals/status")', '@router.post("/agent/terminals/report")'):
        assert r in src
    assert "_mtls_gate" in src and 'router = APIRouter(prefix="/vps"' in src
    server = _read("backend", "server.py")
    assert "include_router(vps_terminal_router)" in server and '@api_router.get("/setup/agent.ps1")' in server
    assert '"vps_terminal": account.get("vps_terminal") or None' in _read("backend", "install_progress.py")
    panel = _read("frontend", "src", "components", "InstallProgressPanel.jsx")
    assert "p.vps_terminal" in panel and "install-vps-terminal-" in panel
    qi = _read("frontend", "src", "components", "QuickInstallPanel.jsx")
    assert "<VpsAgentInstall accountId={accountId} />" in qi
    ui = _read("frontend", "src", "components", "VpsAgentInstall.jsx")
    assert "/vps/agents" in ui and "install-terminal" in ui and "vps-agent-install-btn-" in ui


def test_agent_script_contract():
    ps = _read("backend", "static", "STOIC-Agent.ps1")
    # enrol → register with the existing agent API; token DPAPI-protected; scheduled task at logon (interactive session)
    assert "/api/infra/agent/register" in ps and "Protect-StoicSecret $reg.agent_token" in ps
    assert "Register-ScheduledTask" in ps and "-AtLogOn" in ps and "-RunLevel Highest" in ps
    # install_terminal: clone golden (no logs, no accounts.dat), hash-pinned installer, -TerminalPath/-NoRestart/-TerminalLogin, /portable start
    assert "robocopy" in ps and '/XF "accounts.dat"' in ps and 'Remove-Item (Join-Path $dir "config\\accounts.dat")' in ps
    assert "installer hash mismatch" in ps and "Install-Stoic -Token" in ps and "-TerminalPath $dir -NoRestart" in ps and "-TerminalLogin $login" in ps
    assert '"/portable", "/config:' in ps
    # password: typed once (SecureString), DPAPI file, only in the first-start ini which is deleted; never sent
    assert "Read-Host" in ps and "-AsSecureString" in ps and "ConvertFrom-SecureString" in ps
    assert "stoic-first-start.ini" in ps and "Remove-Item $firstIni" in ps and "[Login]" in ps
    assert "password" not in ps[ps.index("function Send-StoicHeartbeat"):ps.index("function Start-StoicAgentLoop")].lower()
    # watchdog: dead → start; stale → graceful restart; budget 3/h → restart_loop; never force-kill
    assert "$script:MaxRestartsPerHour = 3" in ps and "$script:StaleS = 180" in ps
    assert '"restart_loop"' in ps and "CloseMainWindow" in ps and "Stop-Process" not in ps
    assert "/api/vps/agent/terminals/status" in ps and "/api/vps/agent/terminals/report" in ps
    # signed command sequence verified with the enrolment command_key
    assert "HMACSHA256" in ps and "signature mismatch" in ps
    # installer accepts the agent's login hint
    inst = _read("backend", "static", "STOIC-Installer.ps1")
    assert '[string]$TerminalLogin = ""' in inst and "if ($TerminalLogin) { $TerminalLogin }" in inst
