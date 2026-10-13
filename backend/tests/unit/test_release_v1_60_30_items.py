"""Release after v1.60.26 — item 1 (risk cap: right fix + Bot Config refuses risk above the cap), item 3 (EA WebRequest
4014 visible on the account card; installer prints both origins), item 5 (edge security headers, dependency-scan
publishing, read-only backup mount for the security worker). Items 2 and 4 are covered by
test_scalp_orphan_config_removal.py and test_security_commit_message_scan.py."""
import asyncio
import json
import os
import sys
import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


# ── item 1 · risk cap ───────────────────────────────────────────────────────────────────────────────────
def _cap(monkeypatch, cap):
    import rules_policy as rp

    async def _caps(db):
        return {"max_risk_pct": cap} if cap is not None else {}
    monkeypatch.setattr(rp, "risk_caps", _caps)


def test_bot_config_refuses_risk_level_explicit_pct_adaptive_cap_and_style_above_the_cap(monkeypatch):
    from fastapi import HTTPException
    import routes.bot_routes as br
    _cap(monkeypatch, 1.0)
    db = FakeDb()
    cur = {"risk_level": "medium", "adaptive_sizing_enabled": True, "adaptive_risk_cap_pct": 2.0}
    _run(br._refuse_risk_above_policy_cap(db, cur, {"risk_level": "medium"}))            # 1.0 % — allowed
    _run(br._refuse_risk_above_policy_cap(db, cur, {"risk_level": "low"}))               # 0.5 % — allowed
    for update in ({"risk_level": "high"}, {"risk_percent": 1.5}, {"adaptive_risk_cap_pct": 3.0}):
        with pytest.raises(HTTPException) as ei:
            _run(br._refuse_risk_above_policy_cap(db, cur, update))
        d = ei.value.detail
        assert ei.value.status_code == 409 and d["code"] == "risk_above_policy_cap" and d["cap_pct"] == 1.0
        assert "Bot Config → Risk level" in d["message"] and "≤ 1%" in d["message"]
    # a style whose preset lifts the risk above the cap is refused too
    import strategy_presets as sp
    monkeypatch.setattr(sp, "get_preset", lambda k: {"config": {"risk_level": "high"}} if k == "fast_scalp" else None)
    with pytest.raises(HTTPException):
        _run(br._refuse_risk_above_policy_cap(db, cur, {"active_preset": "fast_scalp"}))
    assert br.effective_risk_for({"risk_level": "low", "active_preset": "fast_scalp"}, 1.0) == 2.5
    # no active policy cap → nothing is refused
    _cap(monkeypatch, None)
    _run(br._refuse_risk_above_policy_cap(db, cur, {"risk_level": "extreme"}))


def test_close_only_notice_maps_risk_cap_breach_to_lower_risk_fix(monkeypatch):
    import routes.account_routes as ar
    import inventory_projection as ip
    import rules_policy as rp
    db = FakeDb()
    oid = ObjectId()
    _run(db.accounts.insert_one({"_id": oid, "user_id": "u1", "label": "Main", "trading_enabled": True}))

    async def _proj(db_, scope):
        return {"per_account_blocks": {str(oid): ["risk per trade above the policy cap 1.0%"]}, "counts": {"rules_policy": "v2"}}

    async def _active(db_):
        return {"policy_version": "v2", "max_accounts_per_user": 5, "risk_caps": {"max_risk_pct": 1.0}}
    monkeypatch.setattr(ip, "projection", _proj)
    monkeypatch.setattr(rp, "active", _active)
    ar.get_db = lambda: db
    n = _run(ar.close_only_notice(user={"id": "u1"}))
    assert n["fix"] == {"action": "lower_risk", "label": "Lower the bot's risk to ≤ 1% (Bot Config → Risk level)"}
    assert "Keep 5 accounts" not in n["fix"]["label"]


def test_bot_config_page_shows_the_cap_and_disables_levels_above_it():
    fe = _read("frontend", "src", "pages", "BotConfig.jsx")
    assert "policy-risk-cap" in fe and "POLICY CAP · RISK PER TRADE ≤" in fe
    assert "const overCap = cfg.policy_risk_cap_pct != null && Number(p.risk_pct) > Number(cfg.policy_risk_cap_pct)" in fe
    assert "disabled={overCap}" in fe and "risk-over-cap-" in fe
    assert 'out["policy_risk_cap_pct"] = await policy_risk_cap(db)' in _read("backend", "routes", "bot_routes.py")


# ── item 3 · WebRequest 4014 visible ────────────────────────────────────────────────────────────────────
def test_connection_hint_names_the_exact_origin_and_the_www_rule(monkeypatch):
    import install_progress as ip
    import routes.account_routes as ar
    monkeypatch.setattr(ip, "webrequest_url", lambda *a, **k: "https://stoicaibot.com")
    paired = {"mode": "live", "installer_paired_at": "2026-10-13T10:00:00+00:00"}
    h = ar.connection_hint(paired, {"state": "NEVER_CONNECTED", "connected": False})
    assert h["code"] == "webrequest_blocked_likely" and h["url"] == "https://stoicaibot.com"
    assert h["text"].startswith("MT5 is blocking the connection: Tools → Options → Expert Advisors")
    assert "add https://stoicaibot.com (exactly, without www)" in h["text"]
    h2 = ar.connection_hint(paired, {"state": "DISCONNECTED", "connected": False})
    assert h2["code"] == "heartbeat_stale" and "WebRequest error 4014" in h2["text"] and "exactly, without www" in h2["text"]
    assert ar.connection_hint(paired, {"state": "CONNECTED", "connected": True}) is None
    assert ar.connection_hint({"mode": "paper"}, {"state": "NEVER_CONNECTED", "connected": False}) is None
    assert ar.connection_hint({"mode": "live"}, {"state": "NEVER_CONNECTED", "connected": False}) is None     # never paired
    assert "(exactly, with www)" in ar.webrequest_hint("https://www.stoicaibot.com/")
    assert ip._www_note("https://stoicaibot.com") == " (exactly, without www)" and ip._www_note(None) == ""
    fe = _read("frontend", "src", "pages", "Accounts.jsx")
    assert "connection-hint-" in fe and "a.connection_hint.text" in fe and "connection-hint-copy-" in fe


def test_installer_prints_both_origins_and_bumps_version():
    ps = _read("backend", "static", "STOIC-Installer.ps1")
    assert "function Get-StoicWebRequestHosts" in ps and '"://www." + $u.Authority' in ps and "$u.Authority.Substring(4)" in ps
    assert "add BOTH lines" in ps and "foreach ($h in (Get-StoicWebRequestHosts -HeartbeatUrl $heartbeatUrl))" in ps
    assert '$InstallerVersion = "1.11"' in ps


# ── item 5 · daily-report LOW items ────────────────────────────────────────────────────────────────────
def test_edge_sets_security_headers_and_security_worker_sees_backups_read_only(tmp_path):
    caddy = _read("deploy", "Caddyfile")
    assert "?Strict-Transport-Security \"max-age=31536000; includeSubDomains\"" in caddy
    assert "?Content-Security-Policy \"default-src 'self'" in caddy and "frame-ancestors 'none'" in caddy
    nginx_csp = [ln for ln in _read("deploy", "nginx.conf").splitlines() if "Content-Security-Policy" in ln][0]
    assert "default-src 'self'; script-src 'self' https://challenges.cloudflare.com" in nginx_csp      # same policy both layers
    # production runs the Cloudflare overlay, whose Caddyfile is RENDERED — render it offline and compare the header block
    import shutil
    import subprocess
    work = tmp_path / "repo"
    (work / "deploy" / "cloudflare").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, "deploy", "cloudflare", "render.sh"), work / "deploy" / "cloudflare" / "render.sh")
    shutil.copy(os.path.join(ROOT, "deploy", "cloudflare", "ips.txt"), work / "deploy" / "cloudflare" / "ips.txt")
    env = {**os.environ, "PATH": str(tmp_path / "bin") + os.pathsep + os.environ.get("PATH", "")}
    (tmp_path / "bin").mkdir(); (tmp_path / "bin" / "curl").write_text("#!/bin/sh\nexit 7\n"); (tmp_path / "bin" / "curl").chmod(0o755)
    out = subprocess.run(["bash", str(work / "deploy" / "cloudflare" / "render.sh"), "stoicaibot.com", "--cf-only"],
                         capture_output=True, text=True, env=env, cwd=work)
    assert out.returncode == 0, out.stdout + out.stderr
    rendered = (work / "deploy" / "cloudflare" / "Caddyfile").read_text(encoding="utf-8")
    block = lambda text: [ln.strip() for ln in text.split("header {", 1)[1].split("}", 1)[0].splitlines() if ln.strip()]  # noqa: E731
    assert "header {" in rendered and block(rendered) == block(caddy)                                  # byte-identical header set
    assert rendered.index("header {") < rendered.index("reverse_proxy frontend:3000")
    compose = _read("docker-compose.yml")
    sec = compose.split("worker-security:")[1].split("worker-model:")[0]
    assert "STOIC_BACKUP_DIR: /backups" in sec and "${BACKUP_DIR:-./backups}:/backups:ro" in sec


def test_dependency_audit_publish_endpoint_is_gated_bounded_and_normalises(monkeypatch):
    import routes.ops_routes as ops
    db = FakeDb()
    monkeypatch.setattr(ops, "get_db", lambda: db)

    class _Req:
        def __init__(self, body, ok=True):
            self._b, self._ok = body, ok
            self.headers = {}

        async def body(self):
            return self._b
    allowed = {"v": True}

    async def _actor(request):
        return (True, "metrics-token") if allowed["v"] else (False, None)
    monkeypatch.setattr(ops, "_ops_actor", _actor)
    payload = {"kind": "python", "source": "ci", "commit": "abc", "tool": "pip-audit",
               "vulnerabilities": [{"package": "foo", "version": "1.0", "id": "GHSA-x", "score": "7.5", "fix": "1.1"}, "junk"]}
    out = _run(ops.publish_dependency_audit(_Req(json.dumps(payload).encode())))
    assert out["ok"] and out["count"] == 1
    doc = _run(db.platform_state.find_one({"_id": "dependency_audit_python"}))
    assert doc["vulnerabilities"] == [{"package": "foo", "version": "1.0", "id": "GHSA-x", "score": 7.5, "fix": "1.1"}]
    assert doc["published_by"] == "metrics-token" and doc["published_at"]
    assert _run(ops.publish_dependency_audit(_Req(json.dumps({"kind": "rust"}).encode()))).status_code == 400
    assert _run(ops.publish_dependency_audit(_Req(b"x" * 262_145))).status_code == 413
    allowed["v"] = False
    assert _run(ops.publish_dependency_audit(_Req(json.dumps(payload).encode()))).status_code == 401


def test_publisher_normalises_pip_audit_and_npm_audit_and_ci_publishes():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import publish_dependency_audit as pda
    pip = {"dependencies": [{"name": "requests", "version": "2.0", "vulns": [{"id": "PYSEC-1", "fix_versions": ["2.1"]}]},
                            {"name": "clean", "version": "1", "vulns": []}]}
    assert pda.normalise_pip_audit(pip) == [{"package": "requests", "version": "2.0", "id": "PYSEC-1", "score": 7.5, "fix": "2.1"}]
    npm = {"vulnerabilities": {"lodash": {"severity": "high", "range": "<4.17.21", "via": [{"source": 1234, "title": "proto"}],
                                          "fixAvailable": {"name": "lodash", "version": "4.17.21"}}}}
    n = pda.normalise_npm_audit(npm)[0]
    assert n["package"] == "lodash" and n["score"] == 7.5 and n["id"] == "1234" and n["fix"] == "lodash@4.17.21"
    for wf in ("ci.yml", "dependency-audit.yml"):
        y = _read(".github", "workflows", wf)
        assert "publish_dependency_audit.py --kind python --input /tmp/pip-audit.json" in y
        assert "publish_dependency_audit.py --kind frontend --input /tmp/npm-audit.json" in y
        assert "STOIC_METRICS_TOKEN: ${{ secrets.STOIC_METRICS_TOKEN }}" in y
