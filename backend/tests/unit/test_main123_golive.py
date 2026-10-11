"""main123 — go-live hotfix: "Propose approval" was unreachable.

P1  The only remaining violation on a fresh production install is "no approved inventory configuration event —
    production fails closed". The UI disabled hash-propose on violations.length > 0 AND the backend's substring
    filter ("approved configuration event") did not match that text either → nobody could ever record the first
    approval. Now: exact-text APPROVAL_CLEARS set, projection returns approvable / blocking_violations, UI uses it.
2   expectation status line is single-operator aware.
3   inactive seed default profile is not a red defect (greyed "harmless").
4   account card: environment attestation computed BEFORE `_id` is popped (digest binds the account id).
5   Edit button on account cards (label / group / MT5 server)."""
import asyncio
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


async def _noop(*a, **k):
    return None


def _db_one_demo_account() -> FakeDb:
    db = FakeDb()
    aid = ObjectId()
    _run(db.accounts.insert_one({"_id": aid, "user_id": "u1", "label": "demo", "mode": "live", "account_type": "demo",
                                 "server": "OnEquity-Server", "trading_enabled": True, "account_number": "1083440"}))
    _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": str(aid), "active": True}))
    _run(db.bot_configs.insert_one({"user_id": "admin", "account_id": None, "active": False, "is_default": True}))   # seed default
    return db, str(aid)


def test_p1_backend_filter_missed_the_fail_closed_violation():
    import inventory_projection as ip
    assert "approved configuration event" not in ip.V_NO_APPROVED_EVENT          # the old substring test could never match it
    assert ip.blocking_violations([ip.V_NO_APPROVED_EVENT, ip.V_UNAPPROVED_CHANGE]) == []
    assert ip.blocking_violations([ip.V_NO_APPROVED_EVENT, "enabled bots 0 != expected 1"]) == ["enabled bots 0 != expected 1"]


def test_p1_fresh_production_install_is_approvable_and_approve_current_succeeds(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("INVENTORY_APPROVAL_MODE", "single_admin")
    import inventory_projection as ip
    db, aid = _db_one_demo_account()
    _run(db.platform_state.insert_one({"_id": "inventory_expectation", "accounts": 1, "enabled": 1, "bots": 1,
                                       "account_ids": [aid], "demo_only": False, "approved_by": "op@x"}))
    proj = _run(ip.projection(db, None))
    assert proj["violations"] == [ip.V_NO_APPROVED_EVENT], proj["violations"]
    assert proj["approvable"] is True and proj["blocking_violations"] == []
    assert proj["blocking"] is True                                               # production still fails closed until approved
    res = _run(ip.approve_current(db, "op@x", "go-live"))                         # used to 409 inventory_not_approvable
    assert res["pending_approval"]["inventory_hash"] == proj["inventory_hash"]
    monkeypatch.setattr("canonical_decision.bump_authority_version", _noop)
    out = _run(ip.confirm_current(db, "op@x"))
    assert out["violations"] == [] and out["approvable"] is True and out["blocking"] is False


def test_p1_real_violation_still_blocks_approve_current(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    from fastapi import HTTPException
    import inventory_projection as ip
    db, aid = _db_one_demo_account()
    _run(db.platform_state.insert_one({"_id": "inventory_expectation", "accounts": 1, "enabled": 1, "bots": 1,
                                       "account_ids": [aid], "demo_only": False, "approved_by": "op@x"}))
    _run(db.bot_configs.insert_one({"user_id": "u1", "account_id": None, "active": True}))      # ACTIVE account-less bot
    with pytest.raises(HTTPException) as ei:
        _run(ip.approve_current(db, "op@x", "x"))
    assert ei.value.status_code == 409 and ei.value.detail["code"] == "inventory_not_approvable"
    assert ei.value.detail["violations"] == ["1 ACTIVE bot configuration(s) with no account id"]


def test_p1_ui_uses_approvable_and_has_a_component_test():
    src = _read("frontend", "src", "components", "admin", "InventoryGoLivePanel.jsx")
    assert 'disabled={!!busy || !approvable}' in src and 'disabled={!!busy || violations.length > 0}' not in src
    assert 'const approvable = inv.approvable ?? blocking.length === 0;' in src
    assert '"no approved inventory configuration event — production fails closed"' in src       # exact-text fallback
    assert '"inventory changed without an approved configuration event"' in src
    assert 'data-testid="hash-propose-hint"' in src
    t = _read("frontend", "src", "components", "admin", "__tests__", "InventoryGoLivePanel.test.jsx")
    assert "is ENABLED when expectation is approved and the only violation is the missing approved event" in t
    assert 'expect(btn).not.toBeDisabled()' in t


def test_2_single_operator_copy():
    src = _read("frontend", "src", "components", "admin", "InventoryGoLivePanel.jsx")
    line = [ln for ln in src.splitlines() if 'data-testid="expectation-pending"' in ln][0]
    assert 'singleAdmin ? "approve with a fresh step-up (single-operator mode)" : "a DIFFERENT admin must approve"' in line


def test_3_inactive_default_profile_is_greyed_not_red():
    src = _read("frontend", "src", "components", "admin", "InventoryGoLivePanel.jsx")
    assert 'const activeOrphans = (pend.orphan_bots || []).filter((b) => b.active);' in src
    assert 'data-testid="inventory-default-profiles"' in src and "inactive, harmless" in src
    orphans = src.split('data-testid="inventory-orphans"')[1].split("</div>\n            )}")[0]
    assert "activeOrphans.map" in orphans and "inactiveOrphans" not in orphans


def test_4_account_card_attestation_matches_admin_view():
    """Admin → Account Environments computes on the raw doc; the card serializer popped `_id` first, so
    attestation_identity() hashed account_id="" and every attestation looked void (UNATTESTED)."""
    from broker_env import attestation_identity, attested_environment
    from routes.account_routes import _serialize
    from routes.admin_routes import _env_row
    aid = ObjectId()
    acc = {"_id": aid, "user_id": "u1", "label": "demo", "mode": "live", "account_type": "demo", "broker": "OnEquity",
           "server": "OnEquity-Server", "broker_server": "OnEquity-Server", "account_number": "1083440",
           "broker_environment": "DEMO", "creds_version": 1,
           "ea_identity": {"installation_id": "inst_0123456789ab", "broker_server": "OnEquity-Server"},
           "broker_account_id_reported": "1083440", "account_trade_mode": "demo"}
    acc["environment_attestation"] = {"environment": "DEMO", "approved_by": "admin@x", "identity_hash": attestation_identity(acc),
                                      "proof": {"verifier": "ea_heartbeat"}}
    assert attested_environment(acc) == "DEMO"
    admin_row = _env_row(dict(acc))
    card = _serialize(dict(acc))
    assert admin_row["effective"] == "DEMO" and admin_row["attestation_state"] == "valid"
    assert card["environment"] == "DEMO" and card["environment_effective"] == "DEMO"
    assert card["environment_attested"] is True and card["environment_attestation_state"] == "valid"
    assert "environment_attestation" not in card and card["id"] == str(aid)


def test_5_edit_button_and_dialog():
    acc = _read("frontend", "src", "pages", "Accounts.jsx")
    assert 'data-testid={`edit-account-${a.account_number}`}' in acc and "<EditAccountDialog account={editAccount}" in acc
    dlg = _read("frontend", "src", "components", "EditAccountDialog.jsx")
    for tid in ("edit-account-label", "edit-account-group", "edit-account-server", "edit-account-save", "edit-account-cancel"):
        assert f'data-testid="{tid}"' in dlg, tid
    assert "api.patch(`/accounts/${account.id}`, changes)" in dlg and "/accounts/server-suggest" in dlg
    assert "Step-up MFA required" in dlg
