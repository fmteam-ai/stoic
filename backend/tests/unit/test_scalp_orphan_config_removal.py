"""Scalp Fast Path orphan cards — DELETE /api/scalp/config answered 404 "Account not found" for runners whose account
had been deleted, so the cards could never be removed; deleting an account now also removes its scalp configs."""
import asyncio
import os
import sys

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tests.unit.fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeRunner:
    def __init__(self, account_id, user_id, symbol="EURUSD"):
        self.account_id, self.user_id, self.symbol = account_id, user_id, symbol
        self.live_trades = {}


def _wire(monkeypatch, db):
    import routes.scalp_routes as sr
    from scalp import engine
    monkeypatch.setattr(sr, "get_db", lambda: db)
    engine._runners.clear()
    return sr, engine


def test_orphan_runner_owned_via_config_is_hard_deleted(monkeypatch):
    db = FakeDb()
    sr, engine = _wire(monkeypatch, db)
    gone = str(ObjectId())                                           # account no longer exists
    _run(db.scalp_configs.insert_one({"account_id": gone, "user_id": "u1", "symbol": "EURUSD", "enabled": True, "mode": "shadow"}))
    engine._runners[f"{gone}:EURUSD"] = _FakeRunner(gone, "u1")
    out = _run(sr.remove_config(gone, "EURUSD", user={"id": "u1"}))
    assert out == {"ok": True, "removed": f"{gone}:EURUSD", "orphan": True, "configs_deleted": 1}
    assert f"{gone}:EURUSD" not in engine._runners
    assert _run(db.scalp_configs.find_one({"account_id": gone})) is None


def test_orphan_runner_owned_only_in_memory_is_removable_and_strangers_still_get_404(monkeypatch):
    from fastapi import HTTPException
    db = FakeDb()
    sr, engine = _wire(monkeypatch, db)
    gone = str(ObjectId())
    engine._runners[f"{gone}:EURUSD"] = _FakeRunner(gone, "u1")
    with pytest.raises(HTTPException) as ei:                          # not mine → nothing leaks, 404 as before
        _run(sr.remove_config(gone, "EURUSD", user={"id": "someone-else"}))
    assert ei.value.status_code == 404 and f"{gone}:EURUSD" in engine._runners
    assert _run(sr.remove_config(gone, "EURUSD", user={"id": "u1"}))["orphan"] is True
    assert f"{gone}:EURUSD" not in engine._runners


def test_live_account_of_another_user_is_never_touched(monkeypatch):
    from fastapi import HTTPException
    db = FakeDb()
    sr, engine = _wire(monkeypatch, db)
    oid = ObjectId()
    _run(db.accounts.insert_one({"_id": oid, "user_id": "owner"}))
    _run(db.scalp_configs.insert_one({"account_id": str(oid), "user_id": "attacker", "symbol": "EURUSD"}))   # forged ownership row
    with pytest.raises(HTTPException) as ei:
        _run(sr.remove_config(str(oid), "EURUSD", user={"id": "attacker"}))
    assert ei.value.status_code == 404
    assert _run(db.scalp_configs.find_one({"account_id": str(oid)})) is not None


def test_existing_account_path_still_tombstones(monkeypatch):
    db = FakeDb()
    sr, engine = _wire(monkeypatch, db)
    oid = ObjectId()
    _run(db.accounts.insert_one({"_id": oid, "user_id": "u1"}))
    out = _run(sr.remove_config(str(oid), "eurusd", user={"id": "u1"}))
    assert out == {"ok": True, "removed": f"{oid}:EURUSD"}
    cfg = _run(db.scalp_configs.find_one({"account_id": str(oid), "symbol": "EURUSD"}))
    assert cfg["removed"] is True and cfg["enabled"] is False


def test_orphan_with_open_live_scalps_is_refused(monkeypatch):
    from fastapi import HTTPException
    db = FakeDb()
    sr, engine = _wire(monkeypatch, db)
    gone = str(ObjectId())
    r = _FakeRunner(gone, "u1")
    r.live_trades = {"t1": {"state": "OPEN"}}
    engine._runners[f"{gone}:EURUSD"] = r
    with pytest.raises(HTTPException) as ei:
        _run(sr.remove_config(gone, "EURUSD", user={"id": "u1"}))
    assert ei.value.status_code == 409


def test_deleting_an_account_removes_its_scalp_configs_and_runners(monkeypatch):
    import account_deletion as ad
    from scalp import engine

    async def _tomb(db, acc, *, deleted_by):
        return None

    async def _cleanup(db, aid, **_k):
        return {"account_id": aid, "pairing_tokens_removed": 0, "alerts_closed": 0}
    import account_cleanup
    import account_tombstones
    monkeypatch.setattr(account_tombstones, "write_tombstone", _tomb)
    monkeypatch.setattr(account_cleanup, "cleanup_deleted_account", _cleanup)
    db = FakeDb()
    oid = ObjectId()
    aid = str(oid)
    _run(db.accounts.insert_one({"_id": oid, "user_id": "u1"}))
    _run(db.scalp_configs.insert_one({"account_id": aid, "user_id": "u1", "symbol": "EURUSD"}))
    _run(db.scalp_configs.insert_one({"account_id": aid, "user_id": "u1", "symbol": "XAUUSD"}))
    _run(db.scalp_configs.insert_one({"account_id": "other", "user_id": "u1", "symbol": "EURUSD"}))
    engine._runners.clear()
    engine._runners[f"{aid}:EURUSD"] = _FakeRunner(aid, "u1")
    engine._runners["other:EURUSD"] = _FakeRunner("other", "u1")
    out = _run(ad.purge_account(db, {"_id": oid, "user_id": "u1"}, deleted_by="u1"))
    assert out["scalp_configs_removed"] == 2
    assert _run(db.scalp_configs.count_documents({"account_id": aid})) == 0
    assert _run(db.scalp_configs.count_documents({"account_id": "other"})) == 1
    assert list(engine._runners) == ["other:EURUSD"]


def test_status_flags_orphans_and_frontend_labels_them():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    assert 'st["account_missing"] = True' in open(os.path.join(root, "backend", "routes", "scalp_routes.py"), encoding="utf-8").read()
    fe = open(os.path.join(root, "frontend", "src", "pages", "Scalp.jsx"), encoding="utf-8").read()
    assert "scalp-runner-account-missing" in fe and "ACCOUNT DELETED" in fe and "removeRunner(r.account_id, r.symbol, !!r.account_missing)" in fe
