"""Phase 4 — simple status UI (Simple Onboarding Spec U1 U15 O5) + the admin "Deploy latest release" button."""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_u1_account_without_server():
    from models import AccountCreate
    a = AccountCreate(label="x", broker="OnEquity", server="", account_number="1083440", account_type="demo", mode="live")
    assert a.server == ""
    with pytest.raises(ValueError):
        AccountCreate(label="x", broker="OnEquity", server="bad;name", account_number="1083440", account_type="demo", mode="live")
    wz = _read("frontend", "src", "components", "AddAccountWizard.jsx")
    assert 'data-testid="wizard-server-auto-note"' in wz and "!!(brokerName && f.account_number) && !realBlocked" in wz
    assert "{f.server_custom && <Field id=\"wz-server\" label=\"SERVER (optional)\">" in wz


def _acc(**kw):
    now = datetime.now(timezone.utc)
    base = {"_id": ObjectId(), "user_id": "u1", "mode": "live", "label": "demo", "broker": "OnEquity", "account_number": "1083440",
            "server": "OnEquity-Server", "account_type": "demo", "broker_environment": "DEMO", "last_heartbeat": now.isoformat(),
            "ea_identity": {"installation_id": "inst_0123456789ab", "authoritative": True, "trade_mode": "demo", "ea_version": "1.64"},
            "account_trade_mode": "demo"}
    base.update(kw)
    return base


def test_u15_one_line_one_fix(monkeypatch):
    import status_line as sl
    import broker_env
    monkeypatch.setattr(broker_env, "attested_environment", lambda a: a.get("_env", "DEMO"))
    now = datetime.now(timezone.utc)
    s = sl.derive(_acc(last_heartbeat=None), bot_active=False, rules_active=False, now=now)
    assert s["state"] == "connecting" and s["fix"]["action"] == "install"
    s = sl.derive(_acc(last_heartbeat=None, bridge_rejected={"at": now.isoformat(), "reason": "invalid_bridge_token"}), bot_active=False, rules_active=False, now=now)
    assert s["state"] == "connecting" and s["fix"]["action"] == "repair" and "token is rejected" in s["label"]
    s = sl.derive(_acc(last_heartbeat=(now - timedelta(minutes=30)).isoformat()), bot_active=False, rules_active=False, now=now)
    assert s["state"] == "connecting" and s["fix"]["action"] == "repair"
    s = sl.derive(_acc(ea_self_check={"webrequest_ok": False}), bot_active=False, rules_active=False, now=now)
    assert s["fix"]["action"] == "webrequest"
    s = sl.derive(_acc(ea_identity={"authoritative": False, "reason": "broker server mismatch: x"}), bot_active=False, rules_active=False, now=now)
    assert s["fix"]["action"] == "adopt_server"
    s = sl.derive(_acc(ea_identity={"authoritative": False, "reason": "installation unknown"}), bot_active=False, rules_active=False, now=now)
    assert s["fix"]["action"] == "trust"
    s = sl.derive(_acc(), bot_active=False, rules_active=False, now=now)
    assert s["state"] == "ready" and s["fix"]["action"] == "start" and s["label"].startswith("Ready — DEMO")
    s = sl.derive(_acc(), bot_active=True, rules_active=False, now=now)
    assert s["state"] == "trading" and s["fix"] is None
    # rules policy: admission gates Ready; real money waits for the operator
    s = sl.derive(_acc(), bot_active=False, rules_active=True, now=now)
    assert s["state"] == "verified" and s["fix"]["action"] == "wait"
    s = sl.derive(_acc(admission={"state": "admitted"}), bot_active=False, rules_active=True, now=now)
    assert s["state"] == "ready"
    s = sl.derive(_acc(_env="LIVE", admission={"state": "pending_operator"}), bot_active=False, rules_active=True, now=now)
    assert s["state"] == "blocked" and s["fix"]["action"] == "operator" and "operator approval" in s["label"]
    s = sl.derive(_acc(account_trade_mode="real", ea_identity={"authoritative": True, "trade_mode": "real"}), bot_active=False, rules_active=False, now=now)
    assert s["state"] == "blocked" and "real-money" in s["label"]
    s = sl.derive(_acc(mode="paper"), bot_active=False, rules_active=True, now=now)
    assert s["state"] == "ready" and s["fix"]["action"] == "start"
    assert set(s["internals"]) >= {"heartbeat_age_s", "identity_authoritative", "environment_effective", "admission", "bot_active"}
    acc_src = _read("backend", "routes", "account_routes.py")
    assert '@router.get("/{account_id}/status-line")' in acc_src
    fe = _read("frontend", "src", "components", "StatusLine.jsx")
    for tid in ("status-line-", "status-line-fix-", "status-line-why-", "status-line-internals-", "status-line-label-"):
        assert tid in fe, tid
    assert "<StatusLine account={a} onFix=" in _read("frontend", "src", "pages", "Accounts.jsx")
    # every fix opens its dialog ON the card (Start acts inline)
    acc_fe = _read("frontend", "src", "pages", "Accounts.jsx")
    assert "else setStatusFix({ account: a, fix })" in acc_fe and "<StatusFixDialog account={statusFix.account} fix={statusFix.fix}" in acc_fe
    dlg = _read("frontend", "src", "components", "StatusFixDialog.jsx")
    for action in ("install", "repair", "trust", "adopt_server", "webrequest", "operator", "wait"):
        assert f"{action}:" in dlg or f'"{action}"' in dlg, action
    for tid in ("fix-dialog-command-", "fix-dialog-copy-", "fix-dialog-cmd-", 'testid="fix-dialog-trust"', 'testid="fix-dialog-adopt"', "fix-dialog-info-"):
        assert tid in dlg, tid
    assert '"/setup/pairing-token", { account_id: account.id, repair }' in dlg and "/trust-terminal" in dlg and "/adopt-reported-server" in dlg
    assert "<WebRequestGuide accountId={account.id} url={url} force />" in dlg
    assert "force = false" in _read("frontend", "src", "components", "WebRequestGuide.jsx")


def test_o5_no_data_findings_are_info_and_hidden_by_default():
    from security_agent.checks.common import f
    from security_agent.findings import open_or_update
    db = FakeDb()
    nd = f("D1", "no_data:python", "low", "dependencies", "no python dependency scan result stored", {"kind": "python", "no_data": True})
    _run(open_or_update(db, nd))
    _run(open_or_update(db, nd))
    rows = db.security_findings.rows
    assert len(rows) == 1 and rows[0]["status"] == "info" and rows[0]["occurrences"] == 2
    real = f("D1", "requests:CVE-1", "high", "dependencies", "x", {"package": "requests"})
    _run(open_or_update(db, real))
    assert [r["status"] for r in rows] == ["info", "open"]
    src = _read("backend", "routes", "security_agent_routes.py")
    assert 'q["status"] = {"$in": ["open", "contained", "acknowledged"]}' in src      # info never in the default list


def test_deploy_latest_button_wiring():
    src = _read("backend", "routes", "admin_routes.py")
    ep = src.split('@router.post("/admin/deploy/latest")')[1]
    assert "GITHUB_DISPATCH_TOKEN" in ep and "actions/workflows/deploy-production.yml/dispatches" in ep and "releases/latest" in ep
    assert 'require_step_up(db, user, request, "authority_relax")' in ep and "require_admin(user)" in ep
    assert '"deploy_latest_release_dispatched"' in ep and '"deploy_button_not_configured"' in ep
    wf = _read(".github", "workflows", "deploy-production.yml")
    assert "workflow_dispatch:" in wf and "ref:" in wf
    panel = _read("frontend", "src", "components", "admin", "RulesPolicyPanel.jsx")
    assert 'data-testid="deploy-latest-btn"' in panel and 'api.post("/admin/deploy/latest")' in panel
