"""prod v1.60.28 — public /status attestation under a RULES policy: `exp_declared` required the retired fixed-count
expectation (expected_accounts/enabled/bots), which is always None under demo-rules-v2, so INFRA stayed
INVENTORY_NOT_ATTESTED forever although approved_hash == inventory_hash with zero platform violations."""
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Db(FakeDb):
    async def command(self, *_a, **_k):
        return {"ok": 1}


def _setup(monkeypatch, *, rules, approved=True, platform_violations=(), structural=(), account_state="READY"):
    import canonical_decision as cd
    import inventory_projection as ip
    import rules_policy as rp
    from routes import portal_routes as pr
    db = _Db()
    now = datetime.now(timezone.utc).isoformat()
    aid = ObjectId()
    _run(db.accounts.insert_one({"_id": aid, "user_id": "u1", "mode": "live", "trading_enabled": True, "last_heartbeat": now}))
    _run(db.bot_configs.insert_one({"account_id": str(aid), "user_id": "u1", "active": True, "_last_pulse": {"ts": now}}))
    monkeypatch.setattr(pr, "get_db", lambda: db)

    async def _proj(db_, scope=None, **_k):
        return {"as_of": now, "counts": {"accounts": 1, "enabled": 1, "bots": 1,
                                         "expected_accounts": None, "expected_enabled": None, "expected_bots": None,
                                         "rules_policy": rules.get("policy_version") if rules else None},
                "accounts": [{"account_id": str(aid), "enabled": True}],
                "violations": list(platform_violations) + list(structural), "platform_violations": list(platform_violations),
                "structural_defects": list(structural), "per_account_blocks": {},
                "approved_hash": "h" * 64 if approved else None, "inventory_hash": "h" * 64}

    async def _active(db_):
        return rules

    async def _version(db_):
        return 7

    async def _platform(db_):
        return {"state": "READY", "new_exposure_allowed": True, "dominant_code": "READY", "decision_id": "dec_p"}

    async def _account(db_, acc):
        return {"state": account_state}
    monkeypatch.setattr(ip, "projection", _proj)
    monkeypatch.setattr(rp, "active", _active)
    monkeypatch.setattr(cd, "authority_version", _version)
    monkeypatch.setattr(cd, "decide_platform", _platform)
    monkeypatch.setattr(cd, "decide_account", _account)
    return pr


def _rules(expired=False):
    exp = datetime.now(timezone.utc) + timedelta(days=-1 if expired else 20)
    return {"policy_version": "demo-rules-v2", "expires_at": exp.isoformat(), **({"expired": True} if expired else {})}


def test_active_rules_policy_counts_as_declared_expectation(monkeypatch):
    pr = _setup(monkeypatch, rules=_rules())
    d = _run(pr._compute_status(time.monotonic()))
    att = d["trading"]["attestation"]
    assert att["attested"] is True and att["basis"] == "signed_rules_policy" and att["input_version"] == 7
    assert d["trading"]["readiness"]["state"] == "READY" and d["trading"]["readiness"]["dominant_code"] == "READY"
    assert d["overall"] == "operational"


def test_no_policy_or_expired_policy_is_not_declared(monkeypatch):
    pr = _setup(monkeypatch, rules=None)
    d = _run(pr._compute_status(time.monotonic()))
    assert d["trading"]["attestation"]["attested"] is False and d["trading"]["attestation"]["basis"] == "platform_availability_only"
    assert d["trading"]["readiness"]["dominant_code"] == "INVENTORY_NOT_ATTESTED" and d["overall"] == "degraded"
    pr = _setup(monkeypatch, rules=_rules(expired=True))
    d = _run(pr._compute_status(time.monotonic()))
    assert d["trading"]["attestation"]["attested"] is False and d["trading"]["readiness"]["dominant_code"] == "INVENTORY_NOT_ATTESTED"


def test_rules_policy_never_overrides_the_other_attestation_invariants(monkeypatch):
    # unapproved hash → not attested
    pr = _setup(monkeypatch, rules=_rules(), approved=False)
    assert _run(pr._compute_status(time.monotonic()))["trading"]["attestation"]["attested"] is False
    # platform-wide violation → not attested
    pr = _setup(monkeypatch, rules=_rules(), platform_violations=["unapproved inventory change"])
    assert _run(pr._compute_status(time.monotonic()))["trading"]["attestation"]["attested"] is False
    # structural defect → not attested
    pr = _setup(monkeypatch, rules=_rules(), structural=["bot without account"])
    assert _run(pr._compute_status(time.monotonic()))["trading"]["attestation"]["attested"] is False
    # an enabled account that is not READY → the per-account check still gates the claim
    pr = _setup(monkeypatch, rules=_rules(), account_state="BLOCKED")
    d = _run(pr._compute_status(time.monotonic()))
    assert d["trading"]["attestation"]["attested"] is False and d["trading"]["attestation"]["basis"] == "signed_rules_policy"
    assert d["trading"]["readiness"]["dominant_code"] == "INVENTORY_NOT_ATTESTED"


def test_per_account_ready_check_is_bounded_and_short_circuits(monkeypatch):
    """Public endpoint with a 20 s budget: decisions run 16-wide and stop at the first non-READY chunk — a
    database with hundreds of enabled accounts must not time the /status probe out (preview: 244 enabled)."""
    import canonical_decision as cd
    import inventory_projection as ip
    pr = _setup(monkeypatch, rules=_rules())
    ids = [str(ObjectId()) for _ in range(100)]
    db = pr.get_db()
    for aid in ids:
        _run(db.accounts.insert_one({"_id": ObjectId(aid), "user_id": "u1", "mode": "live", "trading_enabled": True}))
    now = datetime.now(timezone.utc).isoformat()

    async def _proj(db_, scope=None, **_k):
        return {"as_of": now, "counts": {"rules_policy": "demo-rules-v2"}, "accounts": [{"account_id": a, "enabled": True} for a in ids],
                "violations": [], "platform_violations": [], "structural_defects": [], "per_account_blocks": {},
                "approved_hash": "h" * 64, "inventory_hash": "h" * 64}
    calls = []

    async def _account(db_, acc):
        calls.append(str(acc["_id"]))
        return {"state": "BLOCKED"}
    monkeypatch.setattr(ip, "projection", _proj)
    monkeypatch.setattr(cd, "decide_account", _account)
    d = _run(pr._compute_status(time.monotonic()))
    assert d["trading"]["attestation"]["attested"] is False and len(calls) == 16
    # all READY → every enabled account is evaluated, attested
    calls.clear()

    async def _ready(db_, acc):
        calls.append(1)
        return {"state": "READY"}
    monkeypatch.setattr(cd, "decide_account", _ready)
    d = _run(pr._compute_status(time.monotonic()))
    assert d["trading"]["attestation"]["attested"] is True and len(calls) == 100
