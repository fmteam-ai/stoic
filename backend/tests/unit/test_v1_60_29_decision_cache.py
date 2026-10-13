"""v1.60.29 Decision Cache — per-account and platform decisions are served from the persisted snapshot cache
(db.canonical_decisions) for a few seconds, under the same version / fingerprint / TTL discipline as decide_user, so
status pages and the public /status probe stay fast under load while every input change still invalidates."""
import asyncio
import os
import sys
from datetime import datetime, timezone

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _patch_compute(monkeypatch, level="FULL"):
    import trading_authority as ta
    calls = []

    async def _compute(db, account=None):
        calls.append(str(account["_id"]) if account else "platform")
        return {"level": level, "scope": "account" if account else "platform",
                "account_id": str(account["_id"]) if account else None,
                "domains": {"platform": {"level": level, "reason": "ok"}},
                "computed_at": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(ta, "compute_authority", _compute)
    return calls


def _db_with_account():
    db = FakeDb()
    acc = {"_id": ObjectId(), "user_id": "u1", "label": "Live", "mode": "live", "trading_enabled": True}
    _run(db.accounts.insert_one(dict(acc)))
    return db, acc


def test_account_decision_is_cached_for_the_ttl_and_fresh_bypasses(monkeypatch):
    import canonical_decision as cd
    calls = _patch_compute(monkeypatch)
    db, acc = _db_with_account()
    d1 = _run(cd.decide_account(db, acc))
    d2 = _run(cd.decide_account(db, acc))
    assert d1["state"] == "READY" and d2["decision_id"] == d1["decision_id"] and calls == [str(acc["_id"])]
    cached = _run(db.canonical_decisions.find_one({"_id": f"acct:{acc['_id']}"}))
    assert cached and cached["decision"]["decision_id"] == d1["decision_id"] and "input_version" in cached
    d3 = _run(cd.decide_account(db, acc, fresh=True))
    assert d3["decision_id"] != d1["decision_id"] and len(calls) == 2


def test_account_cache_invalidates_on_version_bump_and_on_account_input_change(monkeypatch):
    import canonical_decision as cd
    calls = _patch_compute(monkeypatch)
    db, acc = _db_with_account()
    _run(cd.decide_account(db, acc))
    _run(cd.bump_authority_version(db, "panic"))
    _run(cd.decide_account(db, acc))
    assert len(calls) == 2                                            # version bump → recomputed
    _run(cd.decide_account(db, acc))
    assert len(calls) == 2                                            # steady state → hit
    toggled = {**acc, "trading_enabled": False}
    _run(cd.decide_account(db, toggled))
    assert len(calls) == 3                                            # account input changed → recomputed
    _run(db.bot_configs.insert_one({"account_id": str(acc["_id"]), "user_id": "u1", "active": True}))
    _run(cd.decide_account(db, toggled))
    assert len(calls) == 4                                            # bot toggled on this account → recomputed


def test_cached_hits_are_copies_so_callers_cannot_corrupt_the_snapshot(monkeypatch):
    import canonical_decision as cd
    _patch_compute(monkeypatch)
    db, acc = _db_with_account()
    d1 = _run(cd.decide_account(db, acc))
    d1["blockers"].append({"code": "INJECTED"})
    d1["state"] = "BLOCKED"
    d2 = _run(cd.decide_account(db, acc))
    assert d2["state"] == "READY" and d2["blockers"] == []


def test_platform_decision_is_cached_and_invalidated_by_platform_switches(monkeypatch):
    import canonical_decision as cd
    calls = _patch_compute(monkeypatch)
    db = FakeDb()
    p1 = _run(cd.decide_platform(db))
    p2 = _run(cd.decide_platform(db))
    assert p1["decision_id"] == p2["decision_id"] and calls == ["platform"]
    _run(db.platform_state.insert_one({"_id": "trading_authority", "level": "CLOSE_ONLY", "set_at": "now"}))
    _run(cd.decide_platform(db))
    assert len(calls) == 2                                            # platform switch flipped → recomputed
    assert _run(cd.decide_platform(db, fresh=True))["decision_id"] != p2["decision_id"]


def test_user_decision_reuses_cached_account_decisions(monkeypatch):
    import canonical_decision as cd
    calls = _patch_compute(monkeypatch)
    db, acc = _db_with_account()
    acc2 = {"_id": ObjectId(), "user_id": "u1", "label": "Live 2", "mode": "live", "trading_enabled": True}
    _run(db.accounts.insert_one(dict(acc2)))
    _run(cd.decide_account(db, acc))                                  # warmed by e.g. the public probe
    d = _run(cd.decide_user(db, "u1"))
    assert d["state"] == "READY" and sorted(a["label"] for a in d["accounts"]) == ["Live", "Live 2"]
    # platform + acc (warm, no recompute) + acc2 (cold) → exactly 3 computations, not 4
    assert sorted(calls) == sorted(["platform", str(acc["_id"]), str(acc2["_id"])])
    _run(cd.decide_user(db, "u1"))
    assert len(calls) == 3                                            # whole user decision served from cache
