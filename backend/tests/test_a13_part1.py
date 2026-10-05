"""A13 audit fix list — Part 1 (during the demo). Run with DB_NAME="" (pure unit)."""
import asyncio
import inspect
import os
import sys
from unittest.mock import patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "unit"))
from fake_mongo import FakeDb  # noqa: E402

pytestmark = pytest.mark.unit


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── A13-1 live crypto kill switch ────────────────────────────────────────────
def test_crypto_live_switch_default_off_blocks_before_any_exchange_call():
    from crypto_bridge import ccxt_engine
    from crypto_bridge.binance_engine import BinanceCCXTEngine
    with patch.dict(os.environ, {"BINANCE_LIVE_ENABLED": "true"}, clear=False):
        os.environ.pop("CRYPTO_LIVE_TRADING_ENABLED", None)
        assert ccxt_engine._live_enabled() is False                     # exchange flag alone is NOT enough
        calls = []
        with patch("crypto_bridge.binance_engine.get_db", lambda: FakeDb()), \
                patch("crypto_bridge.binance_engine.BinanceClient", lambda *a, **k: calls.append("client") or None):
            out = run(BinanceCCXTEngine().execute(user_id="u", account={"_id": ObjectId(), "testnet": False, "mode": "live"},
                                                  signal={"symbol": "BTCUSD", "action": "BUY", "lot_size": 0.01}))
        assert out["blocked"] == "crypto_live_disabled" and calls == []
    with patch.dict(os.environ, {"BINANCE_LIVE_ENABLED": "true", "CRYPTO_LIVE_TRADING_ENABLED": "true"}):
        assert ccxt_engine._live_enabled() is True
    assert "CRYPTO_LIVE_TRADING_ENABLED" in open(os.path.join(os.path.dirname(__file__), "..", ".env.example")).read()


# ── A13-2 no silent hash-key fallback ────────────────────────────────────────
def test_production_refuses_without_dedicated_hash_key_and_migration_report():
    import bridge_tokens as bt
    assert "BRIDGE_TOKEN_HASH_KEY" in bt.production_key_violation({"JWT_SECRET": "x"})
    assert "32" in bt.production_key_violation({"BRIDGE_TOKEN_HASH_KEY": "short"})
    assert "differ" in bt.production_key_violation({"BRIDGE_TOKEN_HASH_KEY": "k" * 40, "JWT_SECRET": "k" * 40})
    assert bt.production_key_violation({"BRIDGE_TOKEN_HASH_KEY": "k" * 40, "JWT_SECRET": "j" * 40}) is None
    with patch.dict(os.environ, {"APP_ENV": "production", "JWT_SECRET": "j" * 40}, clear=False):
        os.environ.pop("BRIDGE_TOKEN_HASH_KEY", None)
        with pytest.raises(RuntimeError):
            bt.token_hash("tok")                                          # no JWT_SECRET fallback in production
    import server
    assert "production_key_violation" in inspect.getsource(server.on_startup)
    import deploy_preflight
    assert "bridge_hash_key" in inspect.getsource(deploy_preflight.run_preflight)
    db = FakeDb()
    db.accounts.rows += [{"_id": ObjectId(), "bridge_token_hash": "h1"}, {"_id": ObjectId(), "bridge_token": "plain"}]
    rep = run(bt.migration_report(db))
    assert rep["hashed_accounts"] == 1 and rep["plaintext_remaining"] == 1 and rep["ok"] is False
    import seed
    assert "migration_report(db)" in inspect.getsource(seed.ensure_indexes)
    from routes import bridge_routes
    src = inspect.getsource(bridge_routes._account_by_token)
    assert 'alert_legacy_token_use(db, acc, "prev")' in src and 'alert_legacy_token_use(db, _owner, "retired")' in src
    _meta = inspect.getsource(bt.alert_legacy_token_use).split("meta={")[1].split("}")[0]
    assert "token" not in _meta                                                      # no token material in the alert


# ── A13-3 Demo Readiness honesty ─────────────────────────────────────────────
def test_manual_ticks_carry_context_and_expire_on_deploy():
    import demo_readiness as dr
    db = FakeDb()
    db.users.rows.append({"_id": ObjectId(), "email": "admin@stoicaibot.com", "two_factor_enabled": True})
    with patch.dict(os.environ, {"ADMIN_EMAIL": "admin@stoicaibot.com", "APP_ENV": "preview"}), \
            patch("modules.pamm.strategy_guard.GIT_COMMIT", "aaaaaaa1"):
        out = run(dr.set_manual(db, "ci_green", True, actor="admin@stoicaibot.com"))
        assert out["build_sha"] == "aaaaaaa1" and out["environment"] == "preview" and out["expires_at"]
        state = {m["id"]: m for m in run(dr.manual_state(db))}
        assert state["ci_green"]["checked"] is True and state["ci_green"]["build_sha"] == "aaaaaaa1"
    # a deploy (new build SHA) invalidates every tick — same DB rows, different context
    with patch.dict(os.environ, {"ADMIN_EMAIL": "admin@stoicaibot.com", "APP_ENV": "preview"}), \
            patch("modules.pamm.strategy_guard.GIT_COMMIT", "bbbbbbb2"):
        state = {m["id"]: m for m in run(dr.manual_state(db))}
        assert state["ci_green"]["checked"] is False and "expired" in state["ci_green"]["expired_reason"]
    # 7-day TTL
    ctx = {"fingerprint": "f"}
    assert dr.tick_valid({"checked": True, "context": "f", "expires_at": "2000-01-01T00:00:00+00:00"}, ctx)[0] is False
    assert dr.tick_valid({"checked": True, "context": "f", "expires_at": "2999-01-01T00:00:00+00:00"}, ctx)[0] is True
    page = open(os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "src", "pages", "DemoReadiness.jsx"), encoding="utf-8").read()
    assert "READY FOR CONTROLLED DEMO TEST" in page and "grants no trading authority" in page and "demo-readiness-authority" in page
    assert "grants_authority\": False" in inspect.getsource(dr.build).replace("'", "\"")
