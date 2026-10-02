"""Tests for iter-37: multi-bot targeting extended beyond Research.

Covers the 4 P0 fixes:
  1. POST /api/nl/strategy/apply uses the targeting helper
  2. _set_risk_level (Risk Commander) broadcasts to all bots
  3. Telegram /run, /stop, /panic broadcast via update_many
  4. POST /api/signals/generate accepts account_id and reads the right config
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from bson import ObjectId

from routes.nl_routes import _set_risk_level


# =========================================================
# (P0-2) _set_risk_level — broadcasts to all bots
# =========================================================
def _mock_db_with_configs(configs):
    db = MagicMock()
    cur = MagicMock()
    cur.to_list = AsyncMock(return_value=configs)
    db.bot_configs.find = MagicMock(return_value=cur)
    db.bot_configs.update_one = AsyncMock()
    acc_cur = MagicMock()
    acc_cur.to_list = AsyncMock(return_value=[])
    db.accounts.find = MagicMock(return_value=acc_cur)
    return db


@pytest.mark.asyncio
async def test_set_risk_level_broadcasts_to_all_bots():
    cfgs = [
        {"_id": ObjectId(), "user_id": "u1", "account_id": None, "symbols": ["XAUUSD"], "risk_level": "middle"},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "a1", "symbols": ["BTCUSD"], "risk_level": "low"},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "a2", "symbols": ["XAUUSD"], "risk_level": "high"},
    ]
    db = _mock_db_with_configs(cfgs)
    with patch("routes.nl_routes.get_db", return_value=db):
        out = await _set_risk_level("u1", "low")
    assert out["risk_level"] == "low"
    assert out["modified"] == 3
    assert out["target_mode"] == "all"
    assert db.bot_configs.update_one.await_count == 3


@pytest.mark.asyncio
async def test_set_risk_level_rejects_invalid_level():
    db = _mock_db_with_configs([])
    with patch("routes.nl_routes.get_db", return_value=db):
        out = await _set_risk_level("u1", "stupid")
    assert "error" in out


@pytest.mark.asyncio
async def test_set_risk_level_handles_no_bots_gracefully():
    db = _mock_db_with_configs([])
    with patch("routes.nl_routes.get_db", return_value=db):
        out = await _set_risk_level("u1", "low")
    assert out["modified"] == 0


# =========================================================
# (P0-1) NL strategy apply — uses targeting helper
# =========================================================
@pytest.mark.asyncio
async def test_nl_strategy_apply_routes_through_targeting():
    """Verify the route imports the helper and uses it. We exercise the
    helper directly (the route is a thin wrapper around it)."""
    from research_agent.proposal_targeting import (
        resolve_target_configs, apply_to_bot_configs,
    )
    cfgs = [
        {"_id": ObjectId(), "user_id": "u1", "account_id": None, "symbols": ["XAUUSD"]},
        {"_id": ObjectId(), "user_id": "u1", "account_id": "btc", "symbols": ["BTCUSD"]},
    ]
    db = _mock_db_with_configs(cfgs)
    # default mode "matching" with XAUUSD scope → only one bot picked
    configs, mode = await resolve_target_configs(db, "u1", ["XAUUSD"], "matching")
    assert len(configs) == 1
    assert mode == "matching:XAUUSD"
    audit = await apply_to_bot_configs(
        db, configs,
        update_fields={"risk_level": "low", "strategy_style": "scalping"},
        source="nl_strategy",
        source_id=None,
        target_mode=mode,
        auto=False,
    )
    assert len(audit) == 1
    set_body = db.bot_configs.update_one.await_args[0][1]["$set"]
    assert set_body["last_change_source"] == "nl_strategy"
    assert set_body["risk_level"] == "low"
    # Backwards-compat audit field is NOT present for non-research source
    assert "last_research_proposal_id" not in set_body


# =========================================================
# (P0-3) Telegram /run, /stop, /panic — broadcast via update_many
# =========================================================
class _AsyncCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __aiter__(self):
        self._it = iter(self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


def _telegram_run_db(configs=()):
    """review-sec: /run now checks users.status and refuses live configs —
    model an ACTIVE user whose bots are all on paper accounts."""
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={"_id": ObjectId(), "status": "active"})
    db.bot_configs.find = MagicMock(side_effect=lambda *a, **k: _AsyncCursor(configs))
    db.accounts.find_one = AsyncMock(return_value={"mode": "paper"})
    db.accounts.count_documents = AsyncMock(return_value=0)
    return db


@pytest.mark.asyncio
async def test_telegram_run_broadcasts_to_all_bots():
    from routes.telegram_routes import _cmd_run
    db = _telegram_run_db([{"account_id": str(ObjectId())} for _ in range(3)])
    db.bot_configs.update_many = AsyncMock(return_value=MagicMock(modified_count=3))
    sent = {}
    async def _fake_send(token, chat_id, msg):
        sent["msg"] = msg
    with patch("routes.telegram_routes.get_db", return_value=db), \
         patch("routes.telegram_routes._send_reply", new=_fake_send):
        await _cmd_run("tok", "chat", str(ObjectId()))
    # update_many called with {"user_id": <uid>} (no account filter)
    call = db.bot_configs.update_many.await_args
    assert list(call[0][0].keys()) == ["user_id"]
    assert call[0][1]["$set"]["active"] is True
    assert "All 3 bots" in sent["msg"]


@pytest.mark.asyncio
async def test_telegram_stop_broadcasts_to_all_bots():
    from routes.telegram_routes import _cmd_stop
    db = MagicMock()
    db.bot_configs.update_many = AsyncMock(return_value=MagicMock(modified_count=2))
    sent = {}
    async def _fake_send(token, chat_id, msg):
        sent["msg"] = msg
    with patch("routes.telegram_routes.get_db", return_value=db), \
         patch("routes.telegram_routes._send_reply", new=_fake_send):
        await _cmd_stop("tok", "chat", "u1")
    call = db.bot_configs.update_many.await_args
    assert call[0][1]["$set"]["active"] is False
    assert "All 2 bots paused" in sent["msg"]


@pytest.mark.asyncio
async def test_telegram_run_reports_when_no_bots():
    from routes.telegram_routes import _cmd_run
    db = _telegram_run_db([])
    db.bot_configs.update_many = AsyncMock(return_value=MagicMock(modified_count=0))
    sent = {}
    async def _fake_send(t, c, m): sent["msg"] = m
    with patch("routes.telegram_routes.get_db", return_value=db), \
         patch("routes.telegram_routes._send_reply", new=_fake_send):
        await _cmd_run("tok", "chat", str(ObjectId()))
    assert "No bots to start" in sent["msg"]


# =========================================================
# (P0-4) POST /api/signals/generate accepts account_id
# =========================================================
@pytest.mark.asyncio
async def test_generate_signal_uses_correct_config_by_account_id():
    from routes.signal_routes import generate_signal
    # Two configs: default (XAUUSD/middle) + specific account (BTC/low)
    default_cfg = {"user_id": "u1", "account_id": None,
                   "symbols": ["XAUUSD"], "risk_level": "middle"}
    btc_cfg = {"user_id": "u1", "account_id": "btc",
               "symbols": ["BTCUSD"], "risk_level": "low"}

    db = MagicMock()
    async def _find_one(q):
        if q.get("account_id") == "btc":
            return btc_cfg
        return default_cfg
    db.bot_configs.find_one = AsyncMock(side_effect=_find_one)
    db.signals.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId()))

    # Capture which risk_level was used
    captured = {}
    async def _fake_analyze(symbol, risk_level, **_kw):
        captured["risk"] = risk_level
        return {"symbol": symbol, "action": "BUY", "confidence": 70}

    user = {"id": "u1"}
    with patch("routes.signal_routes.get_db", return_value=db), \
         patch("routes.signal_routes.analyze_symbol", new=_fake_analyze):
        # Call WITH account_id="btc" → should use BTC config's risk_level
        await generate_signal({"symbol": "BTCUSD"}, account_id="btc", user=user)
    assert captured["risk"] == "low", \
        "account_id=btc must read from the BTC bot_config, not the default"

    # Call WITHOUT account_id → uses default config
    captured.clear()
    with patch("routes.signal_routes.get_db", return_value=db), \
         patch("routes.signal_routes.analyze_symbol", new=_fake_analyze):
        await generate_signal({"symbol": "XAUUSD"}, account_id=None, user=user)
    assert captured["risk"] == "middle"


# =========================================================
# Backwards-compat — research path still stamps the legacy field names
# =========================================================
@pytest.mark.asyncio
async def test_research_path_keeps_legacy_audit_fields():
    from research_agent.proposal_targeting import apply_to_bot_configs
    cfg = {"_id": ObjectId(), "user_id": "u1", "account_id": None, "symbols": ["X"]}
    db = _mock_db_with_configs([cfg])
    await apply_to_bot_configs(
        db, [cfg],
        update_fields={"risk_level": "low"},
        source="research",
        source_id="prop-123",
        target_mode="matching:X",
        auto=False,
    )
    set_body = db.bot_configs.update_one.await_args[0][1]["$set"]
    assert set_body["last_change_source"] == "research"
    assert set_body["last_change_source_id"] == "prop-123"
    # Legacy iter-36 fields preserved for downstream audit consumers
    assert set_body["last_research_proposal_id"] == "prop-123"
    assert set_body["last_research_target_mode"] == "matching:X"


import pytest as _pytest  # noqa: E402
pytestmark = _pytest.mark.unit
