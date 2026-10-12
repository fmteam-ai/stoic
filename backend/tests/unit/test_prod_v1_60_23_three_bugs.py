"""prod v1.60.23 — (2) demo accounts saved as non-demo; (3) one misconfigured account froze trading for everyone;
(1) step-up dialog as a nested Radix dialog (source assertions; the interaction is covered by the browser test)."""
import asyncio
import os
import sys
import types
from datetime import datetime, timezone

import pytest
from bson import ObjectId
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
HASH = "a" * 64


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


def _demo_acc(**kw):
    """RoboForex demo as saved on prod: type left at microcent, no broker_environment, verified terminal, signed EA."""
    now = datetime.now(timezone.utc).isoformat()
    a = {"_id": ObjectId(), "user_id": "u1", "mode": "live", "label": "RoboForex demo", "broker": "RoboForex", "server": "RoboForex-ECN",
         "account_number": "1083440", "account_type": "microcent", "broker_server": "RoboForex-ECN", "broker_account_id_reported": "1083440",
         "last_heartbeat": now, "ea_binary_sha256": HASH, "ea_binary_sha256_method": "installer_attested", "ea_version": "1.64",
         "verified_identity": {"account_number": "1083440"},
         "ea_identity": {"authoritative": True, "installation_id": "inst-1", "trade_mode": "demo", "broker_server": "RoboForex-ECN"}}
    a.update(kw)
    return a


# ── 1 · step-up dialog stacks as a nested modal ─────────────────────────────────────────────────────────
def test_1_step_up_dialog_is_a_radix_dialog():
    fe = _read("frontend", "src", "components", "StepUpDialog.jsx")
    assert 'from "@/components/ui/dialog"' in fe and "<Dialog open onOpenChange=" in fe and "<DialogContent" in fe and 'data-testid="step-up-modal"' in fe
    assert 'className="fixed inset-0 z-[100]' not in fe                 # the old body-level overlay is gone
    assert "onPointerDownOutside={(e) => e.preventDefault()}" in fe      # a click outside never silently cancels the code entry


# ── 2 · demo accounts ───────────────────────────────────────────────────────────────────────────────────
def test_2a_broker_demo_on_signed_verified_ea_declares_demo_automatically(monkeypatch):
    import auto_attest as aa
    from broker_env import broker_environment
    monkeypatch.setattr("ea_capabilities.accepted_ea_sha256s", lambda: [HASH])
    acc = _demo_acc()
    assert broker_environment(acc) == "LIVE"                           # the prod symptom: ENVIRONMENT · LIVE
    d = aa.evaluate(acc, accepted_hashes=[HASH])
    assert d["grant"] is False and d["checks"]["declared_demo"] is False and d["checks"]["broker_reports_demo"] is True and d["declare_demo"] is True
    db = FakeDb()
    _run(db.accounts.insert_one(dict(acc)))
    audits = []

    async def _audit(db_, entry, **k):
        audits.append(entry)
    monkeypatch.setattr(aa, "append_chained", _audit)
    out = _run(aa.apply(db, acc, accepted_hashes=[HASH]))
    doc = _run(db.accounts.find_one({"_id": acc["_id"]}))
    assert doc["broker_environment"] == "DEMO" and doc["declared_environment"] == "demo" and doc["environment_declared_auto"]["source"] == "broker_trade_mode_demo"
    assert out == "granted" and doc["environment_attestation"]["approved_by"] == aa.AUTO_APPROVER      # same heartbeat: declared → attested
    assert [a["action"] for a in audits] == ["account_environment_declared_demo_auto", "account_environment_attest"]
    # never when the owner explicitly declared LIVE, never when the broker says real money, never without the signed EA
    assert aa.evaluate(_demo_acc(broker_environment="LIVE"), accepted_hashes=[HASH])["declare_demo"] is False
    assert aa.evaluate(_demo_acc(ea_identity={"authoritative": True, "installation_id": "i", "trade_mode": "real"}), accepted_hashes=[HASH]).get("declare_demo", False) is False
    assert aa.evaluate(_demo_acc(ea_binary_sha256_method="heartbeat"), accepted_hashes=[HASH])["declare_demo"] is False


def test_2b_create_reflects_the_declared_environment_and_wizard_asks_clearly():
    src = _read("backend", "routes", "account_routes.py")
    assert '"broker_environment": (None if is_paper else ("DEMO" if payload.declared_environment == "demo" or payload.account_type == "demo"' in src
    wiz = _read("frontend", "src", "components", "EasyStartWizard.jsx")
    assert 'account_type: f.env === "demo" ? "demo" : "standard"' in wiz and 'account_type: "microcent"' not in wiz
    assert "is this MT5 login a demo or a real-money account?" in wiz and 'data-testid="wizard-env-hint"' in wiz and "practice money" in wiz and "real money" in wiz


def test_2c_edit_account_type_is_step_up_protected_and_reclassifies(monkeypatch):
    import routes.account_routes as ar
    db = FakeDb()
    monkeypatch.setattr(ar, "get_db", lambda: db)
    steps, audits = [], []

    async def _step(db_, user, request, action):
        steps.append(action)

    async def _audit(db_, uid, action, meta, request, step_up=False):
        audits.append((action, step_up, meta))
    monkeypatch.setattr("step_up.require_step_up", _step)
    monkeypatch.setattr("step_up.audit_event", _audit)

    async def _demo_only(db_):
        return True
    monkeypatch.setattr(ar, "demo_only_policy_active", _demo_only)
    acc = _demo_acc(environment_attestation={"approved_by": "admin@x", "environment": "DEMO"}, broker_environment="LIVE")
    _run(db.accounts.insert_one(dict(acc)))
    user = {"id": "u1", "email": "u@x", "role": "user"}
    out = _run(ar.update_account(str(acc["_id"]), ar.AccountUpdate(account_type="demo"), None, user=user))
    assert steps == ["account_type_edit"] and out["account_type"] == "demo" and out["environment"] == "DEMO"
    doc = _run(db.accounts.find_one({"_id": acc["_id"]}))
    assert doc["broker_environment"] == "DEMO" and doc["declared_environment"] == "demo" and "environment_attestation" not in doc
    assert audits[-1][0] == "account_type_edited" and audits[-1][1] is True and audits[-1][2]["to"] == {"account_type": "demo", "broker_environment": "DEMO"}
    with pytest.raises(HTTPException) as ei:                                   # demo-only period: a LIVE declaration is refused
        _run(ar.update_account(str(acc["_id"]), ar.AccountUpdate(account_type="live"), None, user=user))
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "demo_only_policy"
    assert "account_type_edit" in _read("backend", "step_up.py")
    fe = _read("frontend", "src", "components", "EditAccountDialog.jsx")
    assert 'data-testid="edit-account-type"' in fe and '<option value="demo">' in fe and "{ account_type: acctType }" in fe


# ── 3 · one account's problem blocks only that account ─────────────────────────────────────────────────
def _acc(uid, label, **kw):
    a = {"_id": ObjectId(), "user_id": uid, "label": label, "mode": "live", "trading_enabled": True, "status": "active",
         "account_type": "demo", "broker_environment": "DEMO", "last_heartbeat": datetime.now(timezone.utc).isoformat(),
         "admission": {"state": "admitted"}, "environment_attestation": {"approved_by": "admin@x", "environment": "DEMO"}}
    a.update(kw)
    return a


def _wire_projection(monkeypatch, db, rules):
    import inventory_projection as ip
    import trading_authority as ta

    async def _active(db_):
        return rules
    monkeypatch.setattr("rules_policy.active", _active)
    monkeypatch.setattr(ip, "production_mode", lambda: False)
    monkeypatch.setattr("broker_env.attested_environment", lambda a: "DEMO" if a.get("broker_environment") == "DEMO" else "LIVE")
    monkeypatch.setattr(ta, "production_mode", lambda: False, raising=False)
    return ip, ta


def test_3_unadmitted_enabled_account_blocks_only_itself(monkeypatch):
    db = FakeDb()
    rules = {"policy_version": "demo-rules-v1", "demo_only": True, "max_accounts_per_user": 2, "expired": False}
    ip, ta = _wire_projection(monkeypatch, db, rules)
    healthy = _acc("u1", "OnEquity Demo")
    broken = _acc("u1", "RoboForex demo", admission={"state": "blocked", "reason": "not attested DEMO"}, broker_environment=None)   # enabled, inactive bot, not admitted
    other = _acc("u2", "Other user")
    for a in (healthy, broken, other):
        _run(db.accounts.insert_one(dict(a)))
    for a in (healthy, other):
        _run(db.bot_configs.insert_one({"account_id": str(a["_id"]), "user_id": a["user_id"], "active": True}))
    _run(db.bot_configs.insert_one({"account_id": str(broken["_id"]), "user_id": "u1", "active": False}))
    proj = _run(ip.projection(db))
    assert proj["structural_defects"] == [] and proj["platform_violations"] == [] and proj["blocking"] is False
    blocks = proj["per_account_blocks"]
    assert set(blocks) == {str(broken["_id"])}
    assert any("relation broken" in t for t in blocks[str(broken["_id"])]) and any("not admitted" in t for t in blocks[str(broken["_id"])])
    assert any("not attested DEMO" in t for t in blocks[str(broken["_id"])])
    # authority: only the broken account is CLOSE_ONLY — the owner's healthy account and the other user stay FULL
    assert _run(ta.inventory_domain(db, broken))["level"] == "CLOSE_ONLY"
    assert _run(ta.inventory_domain(db, healthy))["level"] == "FULL"
    assert _run(ta.inventory_domain(db, other))["level"] == "FULL"
    # the per-user notice still names the breach for the owner only
    assert "u1" in proj["per_user_blocks"] and "u2" not in proj["per_user_blocks"]


def test_3_over_cap_blocks_only_the_accounts_beyond_the_cap(monkeypatch):
    db = FakeDb()
    rules = {"policy_version": "demo-rules-v1", "demo_only": True, "max_accounts_per_user": 1, "expired": False}
    ip, ta = _wire_projection(monkeypatch, db, rules)
    first = _acc("u1", "A")
    second = _acc("u1", "B", _id=ObjectId(), admission={"state": "blocked", "reason": "more than 1 enabled account"})
    assert second["_id"] > first["_id"]
    for a in (first, second):
        _run(db.accounts.insert_one(dict(a)))
        _run(db.bot_configs.insert_one({"account_id": str(a["_id"]), "user_id": "u1", "active": True}))
    proj = _run(ip.projection(db))
    assert set(proj["per_account_blocks"]) == {str(second["_id"])} and proj["blocking"] is False
    assert _run(ta.inventory_domain(db, first))["level"] == "FULL" and _run(ta.inventory_domain(db, second))["level"] == "CLOSE_ONLY"


def test_3_ui_and_api_refuse_turning_trading_on_for_an_unadmitted_account(monkeypatch):
    import routes.account_routes as ar
    db = FakeDb()
    monkeypatch.setattr(ar, "get_db", lambda: db)

    async def _active(db_):
        return {"policy_version": "demo-rules-v1", "demo_only": True, "expired": False}
    monkeypatch.setattr("rules_policy.active", _active)
    acc = _acc("u1", "X", trading_enabled=False, admission={"state": "blocked", "reason": "not attested DEMO", "fix": {"action": "attest"}})
    _run(db.accounts.insert_one(dict(acc)))
    with pytest.raises(HTTPException) as ei:
        _run(ar.update_account(str(acc["_id"]), ar.AccountUpdate(trading_enabled=True), None, user={"id": "u1", "email": "u@x", "role": "user"}))
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "not_admitted" and ei.value.detail["message"] == "not attested DEMO"
    _run(db.accounts.update_one({"_id": acc["_id"]}, {"$set": {"admission": {"state": "admitted"}}}))
    out = _run(ar.update_account(str(acc["_id"]), ar.AccountUpdate(trading_enabled=True), None, user={"id": "u1", "email": "u@x", "role": "user"}))
    assert out["trading_enabled"] is True
    fe = _read("frontend", "src", "pages", "Accounts.jsx")
    assert "trading-not-admitted-" in fe and "NOT ADMITTED" in fe and "adm.state !== \"admitted\"" in fe
