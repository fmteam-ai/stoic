"""v1.60.6 — seed.py creates the user-default bot profile (account_id None, active False) on every start.
A fresh install must not fail release-readiness on it: only an ACTIVE account-less bot is a structural defect."""
import asyncio
import os

import pytest

from tests.unit.fake_mongo import FakeDb

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _db_with_null_account_bot(active: bool) -> FakeDb:
    db = FakeDb()
    _run(db.bot_configs.insert_one({"user_id": "admin", "account_id": None, "active": active, "is_default": True}))
    return db


def test_inactive_default_profile_is_not_a_violation(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    import inventory_projection as ip
    proj = _run(ip.projection(_db_with_null_account_bot(active=False)))
    assert proj["violations"] == [] and proj["structural_defects"] == []
    assert proj["blocking"] is False
    assert proj["counts"]["bots_null_account"] == 1          # still counted, never hidden
    assert proj["counts"]["bots_configured_raw"] == 1 and proj["counts"]["bots_active_raw"] == 0


def test_active_null_account_bot_is_blocking(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    import inventory_projection as ip
    proj = _run(ip.projection(_db_with_null_account_bot(active=True)))
    assert proj["violations"] == ["1 ACTIVE bot configuration(s) with no account id"]
    assert proj["structural_defects"] == proj["violations"]
    assert proj["blocking"] is True
    assert proj["counts"]["bots_null_account"] == 1


def test_seed_default_profile_shape_matches_projection_rule():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    seed = open(os.path.join(root, "seed.py")).read()
    assert '"account_id": None' in seed and '"active": False' in seed
    src = open(os.path.join(root, "inventory_projection.py")).read()
    assert 'raw_bots["null_account_active"] += bool(b.get("active"))' in src
    assert 'if raw_bots["null_account_active"] > 0:' in src
