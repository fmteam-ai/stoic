"""prod v1.60.24 — an account switched OFF on purpose (trading_enabled=false) must not dominate the user-level
decision (TRADING BLOCKED strip / authority ribbon), must not degrade INFRA, and the close-only banner must name
ONLY the blocked account."""
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


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _read(*rel):
    return open(os.path.join(ROOT, *rel), encoding="utf-8").read()


def _dec(state, *codes, account_id=None):
    return {"decision_id": "dec_x", "state": state, "account_id": account_id,
            "blockers": [{"domain": "x", "code": c, "state": state, "level": "LOCKED", "reason": c} for c in codes],
            "reason_codes": list(codes), "dominant_code": codes[0] if codes else "READY"}


def _patch_decisions(monkeypatch):
    import canonical_decision as cd

    async def _platform(db):
        return {**_dec("READY"), "scope": "platform"}

    async def _account(db, acc):
        if acc.get("trading_enabled") is True:
            return _dec("READY", account_id=str(acc["_id"]))
        return _dec("BLOCKED", "ACCOUNT_DISABLED", "POSITION_TRUTH_STALE", account_id=str(acc["_id"]))
    monkeypatch.setattr(cd, "decide_platform", _platform)
    monkeypatch.setattr(cd, "decide_account", _account)


# ── 1 · user decision ignores accounts switched OFF on purpose ──────────────────────────────────────────
def test_disabled_account_without_exposure_never_dominates_user_decision(monkeypatch):
    import canonical_decision as cd
    _patch_decisions(monkeypatch)
    db = FakeDb()
    on, off = ObjectId(), ObjectId()
    _run(db.accounts.insert_one({"_id": on, "user_id": "u1", "label": "Live ON", "mode": "live", "trading_enabled": True}))
    _run(db.accounts.insert_one({"_id": off, "user_id": "u1", "label": "Old OFF", "mode": "live", "trading_enabled": False}))
    d = _run(cd._decide_user_uncached(db, "u1"))
    assert d["state"] == "READY" and d["new_exposure_allowed"] is True and d["blockers"] == [] and d["dominant_code"] == "READY"
    rows = {a["label"]: a for a in d["accounts"]}
    assert rows["Live ON"]["counted"] is True and rows["Live ON"]["state"] == "READY"
    assert rows["Old OFF"]["counted"] is False and rows["Old OFF"]["state"] == "OFF" and rows["Old OFF"]["blockers"] == []


def test_disabled_account_with_open_positions_keeps_counting_but_not_its_off_state(monkeypatch):
    import canonical_decision as cd
    _patch_decisions(monkeypatch)
    db = FakeDb()
    on, off = ObjectId(), ObjectId()
    _run(db.accounts.insert_one({"_id": on, "user_id": "u1", "label": "Live ON", "mode": "live", "trading_enabled": True}))
    _run(db.accounts.insert_one({"_id": off, "user_id": "u1", "label": "Old OFF", "mode": "live", "trading_enabled": False}))
    _run(db.trades.insert_one({"account_id": str(off), "status": "open"}))
    d = _run(cd._decide_user_uncached(db, "u1"))
    # exposure is never invisible: the stale truth of the OFF account still blocks — but "it is OFF" is not a blocker
    assert d["state"] == "BLOCKED" and d["reason_codes"] == ["POSITION_TRUTH_STALE"]
    off_row = next(a for a in d["accounts"] if a["label"] == "Old OFF")
    assert off_row["counted"] is True and off_row["enabled"] is False and "ACCOUNT_DISABLED" not in off_row["reason_codes"]


def test_disabled_account_with_broker_reported_positions_counts(monkeypatch):
    import canonical_decision as cd
    _patch_decisions(monkeypatch)
    db = FakeDb()
    _run(db.accounts.insert_one({"_id": ObjectId(), "user_id": "u1", "label": "Old OFF", "mode": "live", "trading_enabled": False, "open_positions": 2}))
    d = _run(cd._decide_user_uncached(db, "u1"))
    assert d["accounts"][0]["counted"] is True and d["state"] == "BLOCKED"


# ── 2 · user-level position truth (authority ribbon) ────────────────────────────────────────────────────
def test_contract_position_truth_ignores_disabled_account_without_exposure():
    from state_contract import contract
    db = FakeDb()
    now = datetime.now(timezone.utc).isoformat()
    _run(db.accounts.insert_one({"_id": ObjectId(), "user_id": "u1", "label": "Live ON", "mode": "live", "trading_enabled": True,
                                 "last_heartbeat": now, "open_positions": 0}))
    _run(db.accounts.insert_one({"_id": ObjectId(), "user_id": "u1", "label": "Old OFF", "mode": "live", "trading_enabled": False}))
    sc = _run(contract(db, "u1"))
    assert sc["position_truth"] == "FRESH"
    by = {r["label"]: r for r in sc["accounts"]}
    assert by["Old OFF"]["position_truth"] == "UNKNOWN" and by["Old OFF"]["counted"] is False and by["Live ON"]["counted"] is True


def test_contract_position_truth_counts_disabled_account_with_open_trade():
    from state_contract import contract
    db = FakeDb()
    off = ObjectId()
    _run(db.accounts.insert_one({"_id": ObjectId(), "user_id": "u1", "label": "Live ON", "mode": "live", "trading_enabled": True,
                                 "last_heartbeat": datetime.now(timezone.utc).isoformat(), "open_positions": 0}))
    _run(db.accounts.insert_one({"_id": off, "user_id": "u1", "label": "Old OFF", "mode": "live", "trading_enabled": False,
                                 "last_heartbeat": (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()}))
    _run(db.trades.insert_one({"account_id": str(off), "status": "open"}))
    sc = _run(contract(db, "u1"))
    assert sc["position_truth"] == "STALE"


# ── 3 · INFRA (public status) — only platform-wide violations break the attestation ─────────────────────
def test_public_status_attestation_uses_platform_violations_only():
    src = _read("backend", "routes", "portal_routes.py")
    exact = src.split("exact = (")[1].split(")\n")[0]
    assert 'proj["platform_violations"]' in exact and 'proj["structural_defects"]' in exact
    assert 'not proj["violations"]' not in exact


def test_projection_keeps_disabled_account_defect_out_of_platform_violations():
    import inventory_projection as ip
    db = FakeDb()
    on, off = ObjectId(), ObjectId()
    _run(db.accounts.insert_one({"_id": on, "user_id": "u1", "label": "Live ON", "mode": "live", "trading_enabled": True}))
    _run(db.accounts.insert_one({"_id": off, "user_id": "u1", "label": "Old OFF", "mode": "live", "trading_enabled": False}))
    _run(db.bot_configs.insert_one({"account_id": str(on), "user_id": "u1", "active": True}))
    _run(db.bot_configs.insert_one({"account_id": str(off), "user_id": "u1", "active": True}))      # bot left ON on a disabled account
    proj = _run(ip.projection(db, "u1"))
    assert any("relation broken" in v for v in proj["violations"])
    assert not any("relation broken" in v for v in proj["platform_violations"]) and proj["structural_defects"] == []
    assert str(off) in proj["per_account_blocks"] and str(on) not in proj["per_account_blocks"]


# ── 4 · close-only banner names ONLY the blocked account ────────────────────────────────────────────────
def _notice_setup(monkeypatch, blocks):
    import inventory_projection as ip
    import rules_policy as rp

    async def _proj(db, scope):
        return {"per_account_blocks": blocks, "per_user_blocks": {}, "counts": {"rules_policy": "demo-rules-v1"}}

    async def _active(db):
        return {"policy_version": "demo-rules-v1", "max_accounts_per_user": 1}
    monkeypatch.setattr(ip, "projection", _proj)
    monkeypatch.setattr(rp, "active", _active)


def test_close_only_notice_names_only_the_blocked_account(monkeypatch):
    import routes.account_routes as ar
    db = FakeDb()
    ok, bad, off = ObjectId(), ObjectId(), ObjectId()
    _run(db.accounts.insert_one({"_id": ok, "user_id": "u1", "label": "Main demo", "trading_enabled": True}))
    _run(db.accounts.insert_one({"_id": bad, "user_id": "u1", "label": "Second demo", "trading_enabled": True}))
    _run(db.accounts.insert_one({"_id": off, "user_id": "u1", "label": "Parked", "trading_enabled": False}))
    _notice_setup(monkeypatch, {str(bad): ["beyond the policy cap of 1 enabled account(s) — disable it or an older one"],
                                str(off): ["enabled account/bot relation broken (account_enabled=False, bot_enabled=True)"]})
    ar.get_db = lambda: db
    n = _run(ar.close_only_notice(user={"id": "u1"}))
    assert n["blocked"] is True and n["fix"]["action"] == "remove_account"
    assert n["line"] == "Account «Second demo» is close-only (no new trades): beyond the policy cap of 1 enabled account(s) — disable it or an older one"
    assert "Main demo" not in n["line"] and "Parked" not in n["line"] and "Your accounts" not in n["line"]
    assert [a["label"] for a in n["accounts"]] == ["Second demo"]            # the OFF account is never mentioned
    assert n["reasons"] == ["Second demo: beyond the policy cap of 1 enabled account(s) — disable it or an older one"]


def test_close_only_notice_two_blocked_accounts_and_clean_user(monkeypatch):
    import routes.account_routes as ar
    db = FakeDb()
    a, b = ObjectId(), ObjectId()
    _run(db.accounts.insert_one({"_id": a, "user_id": "u1", "label": "A", "trading_enabled": True}))
    _run(db.accounts.insert_one({"_id": b, "user_id": "u1", "label": "B", "trading_enabled": True}))
    _notice_setup(monkeypatch, {str(a): ["enabled but not admitted by the rules policy"], str(b): ["enabled but not attested DEMO (demo-only policy)"]})
    ar.get_db = lambda: db
    n = _run(ar.close_only_notice(user={"id": "u1"}))
    assert n["line"] == "2 accounts are close-only (no new trades): A, B" and n["fix"]["action"] == "demo_only"
    clean = _run(ar.close_only_notice(user={"id": "u2"}))
    assert clean == {"blocked": False, "reasons": [], "accounts": [], "line": None, "fix": None, "policy_version": "demo-rules-v1"}


# ── 5 · frontend surfaces ───────────────────────────────────────────────────────────────────────────────
def test_frontend_shows_off_accounts_and_per_account_banner_lines():
    strip = _read("frontend", "src", "components", "AuthorityStrip.jsx")
    assert "a.counted === false" in strip and "OFF · disabled on purpose — not counted" in strip
    banner = _read("frontend", "src", "components", "CloseOnlyBanner.jsx")
    assert "notice.accounts" in banner and "close-only-banner-account-" in banner and "your other accounts are not affected" in banner
