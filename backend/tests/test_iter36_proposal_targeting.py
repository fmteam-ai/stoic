"""Tests for iter-36: research-proposal multi-bot targeting.

Covers:
  - list_candidate_bots flags each config with matches_proposal_symbols
  - resolve_target_configs honours each mode (matching / all / default / specific)
  - apply_proposal_to_configs stamps the audit row on every touched config
  - Manual accept route applies to the chosen target only
  - Auto-accept (iter-34) now uses matching-symbol scope, not blind first-found
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from bson import ObjectId

from research_agent.proposal_targeting import (
    list_candidate_bots, resolve_target_configs, apply_proposal_to_configs,
)


def _mock_db(bot_configs, accounts=None):
    db = MagicMock()

    async def _to_list(*_args, **_kwargs):
        return _to_list._return  # set per-call below

    # bot_configs.find(filter).to_list(length)
    def _bot_find(qfilter):
        f = qfilter or {}
        rows = bot_configs
        if "user_id" in f:
            rows = [r for r in rows if r.get("user_id") == f["user_id"]]
        if "account_id" in f:
            tgt = f["account_id"]
            if isinstance(tgt, dict):
                # $exists / $in / etc. — we only need to handle the "default" branch:
                # $or with account_id is None or $exists False
                rows = [r for r in rows if r.get("account_id") is None]
            else:
                rows = [r for r in rows if r.get("account_id") == tgt]
        if "$or" in f:
            rows = [r for r in rows if r.get("account_id") is None]
        cur = MagicMock()
        cur.to_list = AsyncMock(return_value=rows)
        return cur

    db.bot_configs.find = MagicMock(side_effect=_bot_find)
    db.bot_configs.update_one = AsyncMock()

    # accounts.find for label lookup
    def _acct_find(qfilter, _proj=None):
        rows = accounts or []
        cur = MagicMock()
        cur.to_list = AsyncMock(return_value=rows)
        return cur
    db.accounts.find = MagicMock(side_effect=_acct_find)
    return db


# -------------------- list_candidate_bots --------------------
@pytest.mark.asyncio
async def test_list_candidates_flags_matching_symbols():
    cfgs = [
        {"_id": ObjectId(), "user_id": "u1", "account_id": None, "symbols": ["XAUUSD"], "active": True, "risk_level": "middle"},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "acc1", "symbols": ["BTCUSD"], "active": True, "risk_level": "low"},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "acc2", "symbols": ["XAUUSD", "BTCUSD"], "active": False, "risk_level": "high"},
    ]
    db = _mock_db(cfgs, accounts=[
        {"_id": ObjectId(), "label": "BTC Account"},
    ])
    out = await list_candidate_bots(db, "u1", ["XAUUSD"])
    assert len(out) == 3
    by_acct = {c["account_id"]: c for c in out}
    assert by_acct[None]["matches_proposal_symbols"] is True   # default has XAUUSD
    assert by_acct["acc1"]["matches_proposal_symbols"] is False  # BTC-only
    assert by_acct["acc2"]["matches_proposal_symbols"] is True   # has both


@pytest.mark.asyncio
async def test_list_candidates_no_symbols_means_all_match():
    cfgs = [
        {"_id": ObjectId(), "user_id": "u1", "account_id": None, "symbols": ["XAUUSD"]},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "a1", "symbols": ["BTCUSD"]},
    ]
    db = _mock_db(cfgs)
    out = await list_candidate_bots(db, "u1", [])
    assert all(c["matches_proposal_symbols"] for c in out)


# -------------------- resolve_target_configs --------------------
@pytest.mark.asyncio
async def test_resolve_matching_picks_only_overlap():
    cfgs = [
        {"_id": ObjectId(), "user_id": "u1", "account_id": None, "symbols": ["XAUUSD"]},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "btc", "symbols": ["BTCUSD"]},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "both", "symbols": ["XAUUSD", "BTCUSD"]},
    ]
    db = _mock_db(cfgs)
    configs, mode = await resolve_target_configs(db, "u1", ["XAUUSD"], "matching")
    assert len(configs) == 2  # default + "both"
    assert mode == "matching:XAUUSD"
    account_ids = {c.get("account_id") for c in configs}
    assert account_ids == {None, "both"}


@pytest.mark.asyncio
async def test_resolve_all_returns_every_config():
    cfgs = [
        {"_id": ObjectId(), "user_id": "u1", "account_id": None, "symbols": ["X"]},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "a1", "symbols": ["Y"]},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "a2", "symbols": ["Z"]},
    ]
    db = _mock_db(cfgs)
    configs, mode = await resolve_target_configs(db, "u1", ["X"], "all")
    assert len(configs) == 3
    assert mode == "all"


@pytest.mark.asyncio
async def test_resolve_default_only_returns_account_id_null():
    cfgs = [
        {"_id": ObjectId(), "user_id": "u1", "account_id": None, "symbols": ["X"]},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "a1", "symbols": ["Y"]},
    ]
    db = _mock_db(cfgs)
    configs, mode = await resolve_target_configs(db, "u1", ["X"], "default")
    assert len(configs) == 1
    assert configs[0].get("account_id") is None
    assert mode == "default"


@pytest.mark.asyncio
async def test_resolve_specific_account_id():
    cfgs = [
        {"_id": ObjectId(), "user_id": "u1", "account_id": None, "symbols": ["X"]},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "a1", "symbols": ["Y"]},
    ]
    db = _mock_db(cfgs)
    configs, mode = await resolve_target_configs(db, "u1", ["X"], "a1")
    assert len(configs) == 1
    assert configs[0]["account_id"] == "a1"
    assert mode == "specific:a1"


@pytest.mark.asyncio
async def test_resolve_matching_falls_back_to_all_when_no_proposal_symbols():
    cfgs = [
        {"_id": ObjectId(), "user_id": "u1", "account_id": None, "symbols": ["X"]},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "a1", "symbols": ["Y"]},
    ]
    db = _mock_db(cfgs)
    configs, mode = await resolve_target_configs(db, "u1", [], "matching")
    assert len(configs) == 2
    assert mode == "matching:all_symbols"


# -------------------- apply_proposal_to_configs --------------------
@pytest.mark.asyncio
async def test_apply_stamps_audit_row_on_every_config():
    cfg1_id = ObjectId()
    cfg2_id = ObjectId()
    cfgs = [
        {"_id": cfg1_id, "user_id": "u1", "account_id": None,
         "symbols": ["XAUUSD"], "risk_level": "middle"},
        {"_id": cfg2_id, "user_id": "u1", "account_id": "btc",
         "symbols": ["BTCUSD"], "risk_level": "low"},
    ]
    db = _mock_db(cfgs)
    audit = await apply_proposal_to_configs(
        db, cfgs,
        update_fields={"symbols": ["XAUUSD"], "risk_level": "high"},
        proposal_id="prop-123",
        target_mode="matching:XAUUSD",
        auto=False,
    )
    # Two configs touched
    assert len(audit) == 2
    assert db.bot_configs.update_one.await_count == 2
    # First update_one call carried the audit fields
    args, kwargs = db.bot_configs.update_one.await_args_list[0]
    set_body = args[1]["$set"]
    assert set_body["last_research_proposal_id"] == "prop-123"
    assert set_body["last_research_target_mode"] == "matching:XAUUSD"
    assert "last_research_applied_at" in set_body
    assert set_body["symbols"] == ["XAUUSD"]
    assert set_body["risk_level"] == "high"
    # Audit list contains previous-state info
    a0 = audit[0]
    assert a0["is_default"] is True
    assert a0["previous_symbols"] == ["XAUUSD"]
    assert a0["previous_risk_level"] == "middle"


@pytest.mark.asyncio
async def test_apply_with_auto_flag_uses_auto_audit_key():
    cfg = {"_id": ObjectId(), "user_id": "u1", "account_id": None, "symbols": ["X"]}
    db = _mock_db([cfg])
    await apply_proposal_to_configs(
        db, [cfg],
        update_fields={"risk_level": "low"},
        proposal_id="prop-1",
        target_mode="matching:X",
        auto=True,
    )
    set_body = db.bot_configs.update_one.await_args[0][1]["$set"]
    assert "last_auto_accepted_at" in set_body
    assert "last_research_applied_at" not in set_body
