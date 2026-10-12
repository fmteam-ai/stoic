"""Easy Start Wizard spec (Oct 12 2026) — A1 (rules-policy workflow + one-click load), A2 (deploy checks by rules),
A3 (retire the id list), A4 (renewal reminder), A5 (go-live checklist) and the 4-screen wizard state machine."""
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
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _pol(**kw):
    base = {"policy_version": "demo-rules-v3", "demo_only": True, "max_accounts_per_user": 1, "allowed_brokers": ["*"],
            "risk_caps": {"max_risk_pct": 1.0}, "issued_at": (NOW - timedelta(days=23)).isoformat(), "expires_at": (NOW + timedelta(days=7)).isoformat(),
            "installation_id": "inst-prod"}
    base.update(kw)
    return base


# ── A1 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_a1_workflow_signs_rules_from_a_form_and_panel_has_one_click_load():
    wf = _read(".github", "workflows", "rules-policy.yml")
    for inp in ("demo_only", "max_accounts_per_user", "allowed_brokers", "max_risk_pct", "max_daily_loss_pct", "expires_days", "installation_id", "reason"):
        assert f"      {inp}:" in wf, inp
    assert "environment: policy-approval" in wf and "scripts/sign_rules_policy.py" in wf and '--out "policies/${IN_VERSION}.json"' in wf
    assert "${{ inputs." not in wf.split("run: |")[1].split("- name: Commit")[0]          # inputs only via env (SEC-001)
    assert "RELEASE_SIGNER: external" in wf and "git push origin HEAD:main" in wf and "--force" not in wf
    src = _read("backend", "routes", "authority_routes.py")
    for ep in ('@router.post("/policy/load-latest")', '@router.post("/policy/retire-id-list")', '@router.get("/policy/renewal")'):
        assert ep in src
    assert src.count('await require_step_up(db, user, request, "authority_relax")') >= 4
    fe = _read("frontend", "src", "components", "admin", "RulesPolicyPanel.jsx")
    assert 'data-testid="load-latest-policy-btn"' in fe and '"/authority/policy/load-latest"' in fe
    assert 'data-testid="retire-id-list-btn"' in fe and 'data-testid="rules-policy-renewal-banner"' in fe


def test_a1_load_latest_picks_newest_valid_and_never_downgrades(monkeypatch):
    import rules_policy as rp
    db = FakeDb()
    newer, older, bad = _pol(policy_version="v3"), _pol(policy_version="v2", issued_at=(NOW - timedelta(days=40)).isoformat()), _pol(policy_version="bad")

    async def _gh():
        return [{"name": "v2.json", "policy": older}, {"name": "v3.json", "policy": newer}, {"name": "bad.json", "policy": bad}]
    activated = []

    async def _activate(db_, pol, *, actor, source):
        activated.append((pol["policy_version"], source))
        return {"policy_version": pol["policy_version"], "expires_at": pol["expires_at"]}
    monkeypatch.setattr(rp, "fetch_candidates_from_github", _gh)
    monkeypatch.setattr(rp, "problems", lambda pol, now=None: ["signature invalid"] if pol["policy_version"] == "bad" else [])
    monkeypatch.setattr(rp, "activate", _activate)
    out = _run(rp.load_latest(db, actor="ops@x"))
    assert out["activated"] and out["policy_version"] == "v3" and out["source"] == "github" and out["rejected"][0]["name"] == "bad.json"
    assert activated == [("v3", "github")]
    _run(db.platform_state.insert_one({"_id": rp.STATE_ID, "policy_version": "v3", "issued_at": newer["issued_at"]}))
    out = _run(rp.load_latest(db, actor="ops@x"))
    assert out["activated"] is False and "already the newest" in out["reason"] and len(activated) == 1

    async def _none():
        return []
    monkeypatch.setattr(rp, "fetch_candidates_from_github", _none)
    monkeypatch.setattr(rp, "local_candidates", lambda: [])
    out = _run(rp.load_latest(FakeDb(), actor="ops@x"))
    assert out["activated"] is False and out["source"] == "repo" and "no valid" in out["reason"]


# ── A2 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_a2_deploy_checks_rules_not_counts():
    sh = _read("deploy", "update.sh")
    assert 'RECONCILE_EXPECT="${RECONCILE_EXPECT:-rules}"' in sh and "|approved|rules)$" in sh
    assert "production requires RECONCILE_EXPECT in .env" not in sh
    sys.path.insert(0, os.path.join(ROOT, "backend", "ops"))
    import production_reconcile as pr
    assert pr.EXPECT_RULES == "rules"

    class A:  # noqa: D401
        expect, strict, scope_user = "rules", True, "u1"
    probs = pr.preflight(A(), {"APP_ENV": "production", "GIT_SHA": "abc", "LEDGER_ANCHOR_KEY": "k"})
    assert probs == []
    src = _read("backend", "ops", "production_reconcile.py")
    for chk in ("rules_policy_unexpired", "all_enabled_admitted", "all_enabled_brokers_allowed", "all_users_within_cap", "enabled_environments_all_demo"):
        assert f'"{chk}"' in src, chk
    assert "no active rules policy" in src and "expired" in src          # fail closed → REFUSED (exit 2 → rollback)


# ── A3 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_a3_retire_id_list_is_fail_closed_archived_and_audited(monkeypatch):
    import rules_policy as rp
    from fastapi import HTTPException
    audits = []

    async def _audit(db_, entry, **k):
        audits.append(entry)
    monkeypatch.setattr("audit_chain.append_chained", _audit)
    db = FakeDb()
    with pytest.raises(HTTPException) as ei:
        _run(rp.retire_id_list(db, actor="ops@x"))
    assert ei.value.status_code == 409                                      # no rules policy → refused
    _run(db.platform_state.insert_one({"_id": rp.STATE_ID, **_pol()}))
    assert _run(rp.retire_id_list(db, actor="ops@x"))["retired"] is False  # nothing to retire
    _run(db.platform_state.insert_one({"_id": "inventory_expectation", "policy_version": "demo-1x1-v1", "account_ids": ["a1"], "accounts": 1}))
    out = _run(rp.retire_id_list(db, actor="ops@x", reason="Easy Start A3"))
    assert out["retired"] and out["retired_policy_version"] == "demo-1x1-v1" and out["rules_policy"] == "demo-rules-v3"
    assert _run(db.platform_state.find_one({"_id": "inventory_expectation"})) is None
    arch = _run(db.platform_state.find_one({"_id": "inventory_expectation_retired"}))
    assert arch["retired_by"] == "ops@x" and arch["account_ids"] == ["a1"] and arch["replaced_by_rules_policy"] == "demo-rules-v3"
    assert audits[0]["action"] == "inventory_expectation_retired" and audits[0]["meta"]["retired_policy_version"] == "demo-1x1-v1"


# ── A4 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_a4_renewal_reminder_seven_days_with_prefilled_form(monkeypatch):
    import rules_policy as rp
    import policy_expiry_alerts as pea
    monkeypatch.setenv("GITHUB_REPO", "stoic/bot")
    e = rp.expiry(_pol(), NOW)
    assert e["days_left"] == 7 and e["reminder_due"] is True and e["expired"] is False
    assert rp.expiry(_pol(expires_at=(NOW + timedelta(days=8)).isoformat()), NOW)["reminder_due"] is False
    assert rp.expiry(_pol(expires_at=(NOW - timedelta(hours=1)).isoformat()), NOW)["expired"] is True
    pre = rp.renewal_prefill(_pol())
    assert pre["policy_version"] == "demo-rules-v4" and pre["max_accounts_per_user"] == 1 and pre["expires_days"] == 30 and pre["max_risk_pct"] == 1.0
    assert pre["link"] == "https://github.com/stoic/bot/actions/workflows/rules-policy.yml"
    p = pea.plan_rules(_pol(), NOW)
    assert pea.KEY_RULES_EXPIRING in p["active"] and "EXPIRES IN 7 DAY" in p["raise"][0][3] and "rules-policy.yml" in p["raise"][0][3] and "version demo-rules-v4" in p["raise"][0][3]
    assert pea.plan_rules(None, NOW)["raise"] == []
    assert pea.KEY_RULES_EXPIRED in pea.plan_rules(_pol(expires_at=(NOW - timedelta(days=1)).isoformat()), NOW)["active"]
    assert "plan_rules(pol, now)" in _read("backend", "policy_expiry_alerts.py").split("async def evaluate")[1]


# ── A5 ────────────────────────────────────────────────────────────────────────────────────────────────
def test_a5_go_live_checklist_rows_and_fixes():
    src = _read("backend", "routes", "ops_routes.py")
    blk = src.split('@router.get("/ops/go-live-checklist")')[1]
    for rid in ("release_signed", "rules_policy", "workers", "cloudflare", "backups", "admin_2fa"):
        assert f'"id": "{rid}"' in blk, rid
    assert "TRUST_CF_CONNECTING_IP=true" in blk and "deploy/backup.sh" in blk and '"action": "load_latest_policy"' in blk
    assert 'platform_state.replaceOne({_id:\\"last_backup\\"}' in _read("deploy", "backup.sh")
    fe = _read("frontend", "src", "components", "admin", "GoLiveChecklist.jsx")
    assert '"/ops/go-live-checklist"' in fe and "go-live-fix-" in fe and "<GoLiveChecklist />" in _read("frontend", "src", "pages", "AdminBrokers.jsx")


# ── wizard ────────────────────────────────────────────────────────────────────────────────────────────
def _acc(**kw):
    base = {"_id": ObjectId(), "user_id": "u1", "mode": "live", "label": "My demo", "broker": "OnEquity", "account_number": "1083440",
            "trading_enabled": False, "account_type": "demo", "broker_environment": "DEMO", "account_trade_mode": "demo", "created_at": (NOW - timedelta(minutes=10)).isoformat()}
    base.update(kw)
    return base


def _progress(**status):
    ids = ("token", "install", "binary", "heartbeat", "identity")
    return {"steps": [{"id": i, "status": status.get(i, "pending"), "detail": "", "hint": ""} for i in ids]}


def test_wizard_connect_errors_are_one_sentence_one_button():
    import routes.wizard_routes as wz
    acc = _acc(bridge_rejected={"at": (NOW - timedelta(seconds=30)).isoformat()})
    e = wz.connect_error(acc, _progress(token="done"), NOW)
    assert e["code"] == "old_token" and e["text"] == "MT5 is using an old connection key." and [b["action"] for b in e["buttons"]] == ["repair"]
    acc = _acc(ea_identity={"authoritative": False, "reason": "MT5 login mismatch: EA reports 2222222, account configured 1083440"}, last_heartbeat=NOW.isoformat())
    e = wz.connect_error(acc, _progress(token="done", heartbeat="done"), NOW)
    assert e["code"] == "login_mismatch" and e["text"] == "MT5 is logged in to #2222222, not #1083440."
    assert e["buttons"][0]["login"] == "2222222" and e["buttons"][1]["label"] == "I'll log in to #1083440"
    acc = _acc(last_heartbeat=NOW.isoformat(), ea_self_check={"webrequest_ok": False})
    assert wz.connect_error(acc, _progress(token="done"), NOW)["text"] == "MT5 is blocking STOIC."
    acc = _acc(installer_paired_at=(NOW - timedelta(minutes=4)).isoformat())
    e = wz.connect_error(acc, _progress(token="done", install="done"), NOW)
    assert e["code"] == "no_heartbeat" and "Is MT5 open and logged in to #1083440?" in e["text"] and [b["action"] for b in e["buttons"]] == ["check_again", "repair"]
    acc = _acc(account_trade_mode="real", broker_environment="LIVE", last_heartbeat=NOW.isoformat())
    e = wz.connect_error(acc, _progress(token="done"), NOW)
    assert e["code"] == "real_money" and e["text"] == "This is a real-money account." and e["buttons"][0]["action"] == "switch_real"
    assert wz.connect_error(_acc(last_heartbeat=NOW.isoformat()), _progress(token="done", install="done"), NOW) is None


def test_wizard_activate_cap_sentence_with_disable_button_and_screen_derivation():
    import routes.wizard_routes as wz
    rules = _pol()
    proj = {"approved_hash": "h", "inventory_hash": "h"}
    old = {"_id": ObjectId(), "label": "Old account", "account_number": "1", "trading_enabled": True}
    acc = _acc(admission={"state": "blocked", "reason": "accounts above the policy cap of 1"})
    a = wz.activate_state(acc, rules, proj, [old])
    assert a["error"]["text"] == "Your plan allows 1 account — disable Old account to continue."
    assert a["error"]["buttons"][0] == {"action": "disable_account", "label": "Disable it", "account_id": str(old["_id"]), "account_label": "Old account"}
    acc = _acc(admission={"state": "admitted"}, environment_attestation={"environment": "DEMO", "approved_by": "system:auto-attest"})
    a = wz.activate_state(acc, rules, proj, [])
    assert [t["done"] for t in a["ticks"]] == [True, True, True] and a["done"] is True and a["error"] is None
    acc = _acc(admission={"state": "pending_operator", "reason": "real money"}, account_trade_mode="real", broker_environment="LIVE")
    assert wz.activate_state(acc, rules, proj, [])["waiting_operator"] is True
    assert wz.activate_state(_acc(), None, proj, [])["error"]["code"] == "no_rules"                       # fail closed, explained
    # screen derivation
    full = _progress(token="done", install="done", binary="done", heartbeat="done", identity="done")
    assert wz.current_screen(_acc(), _progress(token="done"), {"done": False}, False) == 2
    assert wz.current_screen(_acc(), full, {"done": False}, False) == 3
    assert wz.current_screen(_acc(), full, {"done": True}, False) == 4
    assert wz.current_screen(_acc(admission={"state": "admitted"}), full, {"done": True}, True) == 5


def test_wizard_cap_counts_only_enabled_accounts_so_disable_is_a_real_fix():
    import rules_policy as rp
    acc = _acc()
    q = rp.cap_count_query(acc)
    assert q["$or"] == [{"trading_enabled": True, "_id": {"$lt": acc["_id"]}}, {"_id": acc["_id"]}] and q["mode"] == {"$ne": "paper"}
    assert 'a.get("trading_enabled") is True' in _read("backend", "inventory_projection.py").split("by_user[a.get")[0][-200:]
    fe = _read("frontend", "src", "components", "EasyStartWizard.jsx")
    for tid in ("wizard-topbar", "wizard-screen-broker", "wizard-screen-connect", "wizard-screen-activate", "wizard-screen-start", "wizard-screen-done",
                "wizard-checklist", "wizard-error", "wizard-risk-cards", "wizard-start-btn", "wizard-copy-btn", "wizard-mini-guide"):
        assert f'data-testid="{tid}"' in fe, tid
    assert "setInterval(refresh, 2000)" in fe and "/setup/wizard/${account.id}" in fe
    acc_fe = _read("frontend", "src", "pages", "Accounts.jsx")
    assert "ADD BROKER" in acc_fe and 'data-testid="accounts-empty-add-broker"' in acc_fe and "continue-setup-" in acc_fe
