"""Security audit #12 — P3 hardenings (VPS Agent command binding, enrolment throttle, mute step-up, signer headers)."""
import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8-sig").read()


class _Agents:
    def __init__(self, agent):
        self.agent = agent

    async def find_one(self, flt, **k):
        return self.agent

    async def find_one_and_update(self, flt, upd, **k):
        self.agent["command_seq"] = self.agent.get("command_seq", 0) + 1
        return self.agent


class _Cmds:
    def __init__(self):
        self.docs = []

    async def insert_one(self, d):
        self.docs.append(d)


def test_p3_command_signature_binds_params(monkeypatch):
    import vps_pathb as pb
    import vps_agent
    monkeypatch.setattr(vps_agent, "agent_command_key", lambda a: "k" * 32)
    db = type("DB", (), {})()
    db.vps_agents = _Agents({"_id": "x", "agent_id": "agt_1", "user_id": "u1", "command_seq": 0})
    db.agent_commands = _Cmds()
    params = {"pairing_token": "T", "server_url": "https://s", "login": "1"}
    res = asyncio.new_event_loop().run_until_complete(pb.queue_command(db, "u1", "agt_1", "install_terminal", params, "user:u1"))
    cmd = db.agent_commands.docs[0]
    canon = pb.canonical_params(params).encode("utf-8")
    assert canon == b"login\x001\npairing_token\x00T\nserver_url\x00https://s\n"
    ph = hashlib.sha256(canon).hexdigest()
    assert cmd["params_sha256"] == ph
    assert cmd["sig_v2"] == hmac.new(b"k" * 32, f"agt_1|{res['command_id']}|1|install_terminal|{ph}".encode(), hashlib.sha256).hexdigest()
    assert cmd["sig"] == hmac.new(b"k" * 32, f"agt_1|{res['command_id']}|1|install_terminal".encode(), hashlib.sha256).hexdigest()   # legacy agents
    tampered = {**params, "server_url": "https://evil"}
    assert hashlib.sha256(pb.canonical_params(tampered).encode()).hexdigest() != ph
    assert pb.canonical_params({"b": None, "a": True, "Z": 1}) == "Z\x001\na\x00true\nb\x00\n"          # ordinal order, None → "", bool lower
    src = _read("backend", "vps_pathb.py")
    assert '"sig_v2": c.get("sig_v2"), "params_sha256": c.get("params_sha256")' in src                     # delivered on poll
    ps = _read("backend", "static", "STOIC-Agent.ps1")
    assert "function ConvertTo-StoicCanonicalParams" in ps and "|$paramsHash" in ps and "has no sig_v2" in ps
    assert "[StringComparer]::Ordinal" in ps and "Append([char]0)" in ps


@pytest.mark.skipif(not (os.path.exists("/usr/bin/pwsh") or os.path.exists("/usr/local/bin/pwsh")), reason="pwsh not installed")
def test_p3_canonical_params_match_python():
    import vps_pathb as pb
    ps = _read("backend", "static", "STOIC-Agent.ps1")
    fn = ps[ps.index("function ConvertTo-StoicCanonicalParams"):ps.index("function Invoke-StoicCommands")]
    params = {"server_url": "https://s", "Login": "1", "directory": "C:\\STOIC\\MT5\\account-1\\", "flag": True, "none": None, "n": 2}
    expected = pb.canonical_params(params)
    out = subprocess.run(["pwsh", "-NoProfile", "-Command", fn + f"\n$p = '{json.dumps(params)}' | ConvertFrom-Json; [Console]::Out.Write((ConvertTo-StoicCanonicalParams $p))"],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout == expected, out.stderr


def test_p3_enrolment_code_space_and_register_throttle():
    import vps_pathb as pb
    import re
    code = pb._enrollment_code()
    assert re.fullmatch(r"[A-Z]{4}-\d{4}", code)
    src = _read("backend", "routes", "infra_routes.py")
    reg = src[src.index('@router.post("/agent/register")'):src.index('@router.post("/agent/heartbeat")')]
    assert 'rate_limit(get_db(), "agent_register", client_ip(request), 20, 600' in reg


def test_p3_mute_requires_step_up_and_signer_headers():
    src = _read("backend", "routes", "ops_routes.py")
    mute = src[src.index('@router.post("/ops/alerts/mute")'):src.index('@router.post("/ops/alerts/ack-all")')]
    assert mute.count("_ops_admin_step_up(request") == 2 and "_ops_actor(request)" not in mute
    signer = _read("deploy", "signer", "app.py")
    assert 'Strict-Transport-Security' in signer and 'X-Content-Type-Options' in signer
    assert "--no-server-header" in _read("deploy", "signer", "Dockerfile")
