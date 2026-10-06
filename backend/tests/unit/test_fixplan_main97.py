"""main97 review — N97-1..11, Q-1, Q-2 corrections (pure unit, FakeDb)."""
import asyncio
import inspect
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_mongo import FakeCollection, FakeDb  # noqa: E402

pytestmark = pytest.mark.unit
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _src(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _client(**kw):
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.amount_to_precision = lambda s, a: float(a)
    c.create_market_order = AsyncMock(return_value={"id": "FL", "status": "closed", "average": 60000.0})
    c.place_oco_protection = AsyncMock(return_value={"list_id": "L1", "list_client_order_id": "x-oco",
                                                     "tp_client_order_id": "x-tp", "sl_client_order_id": "x-sl"})
    c.fetch_order_by_client_id = AsyncMock(return_value=None)
    c.fetch_oco_status = AsyncMock(return_value=None)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


# ── N97-1 daily cap per symbol, auto only, default 1 ─────────────────────────
def test_daily_cap_is_per_symbol_auto_only_default_one():
    import account_reservations as ar
    db = FakeDb()
    aid = str(ObjectId())
    today = datetime.now(timezone.utc).isoformat()
    db.trades.rows.append({"user_id": "u", "account_id": aid, "status": "closed", "origin": "auto",
                           "symbol": "XAUUSD-ECN", "opened_at": today})
    caps = run(ar.caps_for(db, user_id="u", cfg_account_id=aid, max_concurrent=3))
    assert caps["daily_cap"] == 1                                  # unset → 1 per symbol (bot_runner default)
    # XAUUSD (suffixed spelling counted) is capped; EURUSD is a different symbol → allowed
    r_x = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="mt5", decision_id="x1",
                               caps=caps, symbol="XAUUSD", origin="auto"))
    assert r_x["ok"] is False and r_x["blocked"] == "trade_of_day_cap" and r_x["today"] == 1
    r_e = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="mt5", decision_id="e1",
                               caps=caps, symbol="EURUSD", origin="auto"))
    assert r_e["ok"] is True
    # an active EURUSD reservation now counts toward EURUSD's daily cap
    r_e2 = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="mt5", decision_id="e2",
                                caps=caps, symbol="EURUSD", origin="auto"))
    assert r_e2["ok"] is False and r_e2["blocked"] == "trade_of_day_cap"
    # MANUAL / test trades are never bound by the automated caps
    r_m = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="mt5", decision_id="m1",
                               caps=caps, symbol="XAUUSD", origin="manual"))
    assert r_m["ok"] is True
    db.bot_configs.rows.append({"user_id": "u", "account_id": aid, "trade_of_day_cap": 0, "max_concurrent_trades": 4})
    caps0 = run(ar.caps_for(db, user_id="u", cfg_account_id=aid, max_concurrent=None))
    assert caps0["daily_cap"] == 0 and caps0["auto_cap"] == 4      # 0 = unlimited; cap read from config


def test_manual_entry_hits_only_the_hard_total_cap_with_its_own_code():
    import account_reservations as ar
    snap = {"total_open": 4, "auto_open": 4, "today_auto": 9, "reserved_risk_usd": 0}
    caps = {"auto_cap": 2, "total_cap": 4, "daily_cap": 1}
    assert [c for c, _ in ar._violations(snap, caps, 0, "manual")] == ["max_concurrent_cap"]
    assert {c for c, _ in ar._violations(snap, caps, 0, "auto")} == {"max_concurrent_cap", "trade_of_day_cap"}
    assert ar._violations({**snap, "total_open": 3}, caps, 0, "manual") == []


# ── N97-2 scalp goes through the account-wide service ────────────────────────
def test_scalp_reserves_through_reserve_entry_with_caps():
    src = _src("backend", "scalp", "engine.py")
    assert "_ar.reserve_entry(" in src and "_ar.caps_for(" in src and 'source="scalp"' in src
    assert "risk_reservations.reserve(" not in src.split("_ar.reserve_entry(")[1][:3000]
    assert '"refused_caps"' in src


# ── N97-3 signed decision fields ─────────────────────────────────────────────
def test_bundle_signature_covers_verdict_accounts_release_and_expiry():
    import acceptance_bundle as ab
    from test_fixplan_main99_phase4 import signer_env
    with patch.dict(os.environ, signer_env()):
        from release_signing import key_id
        doc = {"bundle_id": "b1", "payload": {"x": 1}, "verdict": "FAIL", "failures": ["f"], "account_ids": ["a"],
               "build_sha": "abc", "image_digest": None, "created_by": "me", "created_at": "t", "expires_at": "2099",
               "schema_version": ab.SCHEMA_VERSION, "key_id": key_id(), "algo": "ed25519", "config_fingerprint": "fp"}
        doc["digest"], doc["signature"] = ab._sign(doc)
        assert ab.verify_signature(doc)
        for k, v in (("verdict", "PASS"), ("account_ids", ["a", "b"]), ("expires_at", "2199"),
                     ("build_sha", "other"), ("image_digest", "sha256:x"), ("failures", [])):
            assert ab.verify_signature({**doc, k: v}) is False, k
        assert ab.bundle_covers({**doc, "verdict": "PASS"}, "a", {"build_sha": "abc", "image_digest": None})[1] \
            == "acceptance bundle signature invalid"


# ── N97-4 dual-verify legacy JWT_SECRET hashes ───────────────────────────────
def test_legacy_jwt_secret_hash_still_matches_and_is_rehashed_once():
    import bridge_tokens as bt
    db = FakeDb()
    env = {"JWT_SECRET": "j" * 40, "BRIDGE_TOKEN_HASH_KEY": "", "APP_ENV": "preview"}
    with patch.dict(os.environ, env):
        old_hash = bt.token_hash("tok_abc")                           # hashed under the fallback
    db.accounts.rows.append({"_id": ObjectId(), "bridge_token_hash": old_hash, "label": "EA-1"})
    with patch.dict(os.environ, {**env, "BRIDGE_TOKEN_HASH_KEY": "k" * 40}):
        assert bt.legacy_token_hash("tok_abc") == old_hash
        acc = run(bt.find_by_current(db, "tok_abc"))
        assert acc is not None and acc["label"] == "EA-1"
        assert db.accounts.rows[0]["bridge_token_hash"] == bt.token_hash("tok_abc")   # re-hashed under the new key
        assert db.accounts.rows[0]["bridge_token_rehashed_at"]
        assert run(bt.find_by_current(db, "tok_abc")) is not None                   # direct hit now
        assert run(bt.find_by_current(db, "tok_other")) is None
    assert "REQUIRED" in _src("docs", "PRODUCTION_DEPLOY_CHECKLIST.md").split("BRIDGE_TOKEN_HASH_KEY")[1][:40]
    env_example = _src("deploy", "env", "backend.env.example")
    assert "BRIDGE_TOKEN_HASH_KEY=" in env_example and "CRYPTO_LIVE_TRADING_ENABLED=" in env_example


# ── N97-5 update.sh stops when the DB restore fails ──────────────────────────
def test_update_sh_leaves_stack_stopped_on_restore_failure():
    sh = _src("deploy", "update.sh")
    assert 'if ! RESTORE_NO_START=1 deploy/backup.sh restore "${PRE_BACKUP}"; then' in sh
    block = sh.split('if ! RESTORE_NO_START=1')[1].split("fi")[0]
    assert "exit 1" in block and "stack left STOPPED" in block
    assert '|| echo "!! database restore FAILED' not in sh


# ── N97-6 testnet without sandbox is refused ─────────────────────────────────
def test_testnet_on_exchange_without_sandbox_is_refused():
    from crypto_bridge import ccxt_engine as ce
    assert ce.sandbox_available({"exchange_id": "binance"}) is True
    assert ce.sandbox_available({"exchange_id": "kraken"}) is False and ce.sandbox_available({"exchange_id": "binanceus"}) is False
    with patch("crypto_bridge.ccxt_engine._decrypt_creds", return_value=("k", "s", None)):
        with pytest.raises(ce.SandboxUnavailable):
            ce._new_exchange({"exchange_id": "kraken", "testnet": True})
    src = inspect.getsource(__import__("crypto_bridge.binance_engine", fromlist=["BinanceCCXTEngine"]).BinanceCCXTEngine._engine_stage)
    assert '"testnet_unavailable"' in src and src.index("testnet_unavailable") < src.index("crypto_live_disabled")


# ── N97-7 / N97-8 flatten id length + sizing from the filled amount ──────────
def test_flatten_id_fits_binance_and_sizing_uses_filled_minus_base_fees():
    from crypto_bridge import crypto_execution as cx
    from execution_intents import new_intent_id
    cid = cx.client_order_id(new_intent_id())
    for suf in ("-fl", "-oco", "-tp", "-sl"):
        assert len(cx.derived_id(cid, suf)) <= 36
    with pytest.raises(ValueError):
        cx.derived_id(cid, "-flatten-way-too-long")
    order = {"filled": 0.5, "fees": [{"currency": "BTC", "cost": 0.0005}, {"currency": "USDT", "cost": 1.2}]}
    assert cx.filled_base_amount(order, "BTC") == pytest.approx(0.4995)
    assert cx.filled_base_amount({"filled": 0.5, "fee": {"currency": "BTC", "cost": 0.001}}, "BTC") == pytest.approx(0.499)
    assert cx.filled_base_amount({}, "BTC") == 0.0
    # flatten uses amount_to_precision + "-fl"
    db = FakeDb()
    tid = ObjectId()
    db.trades.rows.append({"_id": tid, "status": "open", "broker_kind": "binance", "protection": {"status": "MISSING"}})
    c = _client(place_oco_protection=AsyncMock(side_effect=RuntimeError("oco")),
                amount_to_precision=lambda s, a: round(a, 3))
    prot = run(cx.protect(db, c, trade_id=tid, ccxt_symbol="BTC/USDT", side="buy", amount=0.49951,
                          stop_loss=59000, take_profit=62000, cid=cid, account={}))
    assert prot["status"] == cx.PROTECTION_FLATTENED and prot["flatten_amount"] == 0.5
    assert c.create_market_order.await_args.kwargs["client_order_id"] == cid + "-fl"
    assert db.trades.rows[0]["status"] == "closed" and db.trades.rows[0]["exit_price"] == 60000.0


# ── N97-9 recovery / protection gaps ─────────────────────────────────────────
def test_spot_sell_is_not_protected_with_a_buy_oco_and_counts_as_protected():
    from crypto_bridge import crypto_execution as cx
    db = FakeDb()
    tid = ObjectId()
    db.trades.rows.append({"_id": tid, "status": "open", "broker_kind": "binance", "account_id": "a",
                           "action": "SELL", "protection": {"status": "MISSING"}})
    c = _client()
    prot = run(cx.protect(db, c, trade_id=tid, ccxt_symbol="BTC/USDT", side="sell", amount=0.1,
                          stop_loss=1, take_profit=2, cid="stoic-x", account={}))
    assert prot["status"] == cx.PROTECTION_NA and not c.place_oco_protection.await_count
    assert run(cx.unprotected_open_count(db, "a")) == 0


def test_protection_claim_prevents_request_path_and_sweep_double_oco():
    from crypto_bridge import crypto_execution as cx
    db = FakeDb()
    tid = ObjectId()
    db.trades.rows.append({"_id": tid, "status": "open", "broker_kind": "binance", "account_id": "a",
                           "protection": {"status": cx.PROTECTION_PLACING, "at": datetime.now(timezone.utc).isoformat()}})
    c = _client()
    prot = run(cx.protect(db, c, trade_id=tid, ccxt_symbol="BTC/USDT", side="buy", amount=0.1,
                          stop_loss=1, take_profit=2, cid="stoic-x", account={}))
    assert prot["status"] == cx.PROTECTION_PLACING and not c.place_oco_protection.await_count
    acc_id = ObjectId()
    db.trades.rows[0]["account_id"] = str(acc_id)
    db.accounts.rows.append({"_id": acc_id})
    assert run(cx.reconcile_protection(db, lambda a: c))["skipped"] == 1
    # an EXPIRED placing lease is taken over
    db.trades.rows[0]["protection"]["at"] = (datetime.now(timezone.utc) - timedelta(seconds=cx.PLACING_LEASE_SEC + 5)).isoformat()
    prot2 = run(cx.protect(db, c, trade_id=tid, ccxt_symbol="BTC/USDT", side="buy", amount=0.1,
                           stop_loss=1, take_profit=2, cid="stoic-x", account={}))
    assert prot2["status"] == cx.PROTECTION_PLACED and c.place_oco_protection.await_count == 1


def test_filled_limit_becomes_open_and_dispatched_not_found_reaches_failed_confirmed():
    from crypto_bridge import crypto_execution as cx
    db = FakeDb()
    acc_id = ObjectId()
    db.accounts.rows.append({"_id": acc_id})
    it = run(cx.begin(db, account_id=str(acc_id), user_id="u", signal={"symbol": "BTCUSD", "signal_id": "L1",
                                                                       "stop_loss": 1, "take_profit": 2},
                      order_type="limit", ccxt_symbol="BTC/USDT", side="buy", amount=0.2, reservation_id="rsv1"))
    run(cx.record_outcome(db, it["intent_id"], {"id": "E1", "status": "open"}))
    db.trades.rows.append({"_id": ObjectId(), "client_order_id": it["client_order_id"], "status": "pending",
                           "broker_kind": "binance", "account_id": str(acc_id)})
    db.risk_reservations.rows.append({"reservation_id": "rsv1", "state": "RISK_RESERVED", "uncertain": True,
                                      "account_id": str(acc_id), "transition_keys": [], "transitions": []})
    c = _client(fetch_order_by_client_id=AsyncMock(return_value={"id": "E1", "status": "closed", "average": 60500.0,
                                                                 "filled": 0.2, "fees": [{"currency": "BTC", "cost": 0.0002}]}))
    out = run(cx.reconcile_intents(db, lambda a: c))
    t = db.trades.rows[0]
    assert out["reconciled"] == 1 and t["status"] == "open" and t["entry_price"] == 60500.0
    assert t["held_amount"] == pytest.approx(0.1998) and db.execution_intents.rows[0]["status"] == "filled"
    assert db.execution_intents.rows[0]["trade_recorded"] is True
    r = db.risk_reservations.rows[0]
    assert r["state"] == "SLOT_LINKED" and r["uncertain"] is False and r["trade_id"] == str(t["_id"])
    # dispatched + never at the exchange → unknown → failed_confirmed (table forbids the direct jump); reservation released
    it2 = run(cx.begin(db, account_id=str(acc_id), user_id="u", signal={"symbol": "BTCUSD", "signal_id": "L2"},
                       order_type="market", ccxt_symbol="BTC/USDT", side="buy", amount=0.1, reservation_id="rsv2"))
    db.risk_reservations.rows.append({"reservation_id": "rsv2", "state": "RISK_RESERVED", "uncertain": True,
                                      "account_id": str(acc_id), "transition_keys": [], "transitions": []})
    c2 = _client(fetch_order_by_client_id=AsyncMock(return_value=None))
    later = datetime.now(timezone.utc) + timedelta(seconds=cx.UNKNOWN_GRACE_SEC + 1)
    assert run(cx.reconcile_intents(db, lambda a: c2, now=later))["failed_confirmed"] == 1
    i2 = [i for i in db.execution_intents.rows if i["intent_id"] == it2["intent_id"]][0]
    assert i2["status"] == "failed_confirmed"
    assert [r for r in db.risk_reservations.rows if r["reservation_id"] == "rsv2"][0]["state"] == "RELEASED"


def test_crash_between_fill_and_insert_is_recovered_and_filled_oco_closes_trade():
    from crypto_bridge import crypto_execution as cx
    db = FakeDb()
    acc_id = ObjectId()
    db.accounts.rows.append({"_id": acc_id})
    it = run(cx.begin(db, account_id=str(acc_id), user_id="u", signal={"symbol": "BTCUSD", "signal_id": "C1",
                                                                       "stop_loss": 59000, "take_profit": 62000},
                      order_type="market", ccxt_symbol="BTC/USDT", side="buy", amount=0.1))
    run(cx.record_outcome(db, it["intent_id"], {"id": "E9", "status": "closed", "average": 60000.0, "filled": 0.1}))
    assert db.trades.rows == []                                    # crashed before insert_trade_once
    c = _client(fetch_order_by_client_id=AsyncMock(return_value={"id": "E9", "status": "closed", "average": 60000.0, "filled": 0.1}))
    out = run(cx.reconcile_intents(db, lambda a: c))
    assert out["recorded"] == 1 and len(db.trades.rows) == 1 and db.trades.rows[0]["recovered_from_exchange"]
    assert run(cx.reconcile_intents(db, lambda a: c))["recorded"] == 0   # idempotent
    # a filled OCO TP leg closes the local trade with the leg's fill price
    t = db.trades.rows[0]
    t.update({"status": "open", "entry_price": 60000.0, "held_amount": 0.1,
              "protection": {"status": cx.PROTECTION_PLACED, "list_client_order_id": "x-oco",
                             "tp_client_order_id": "x-tp", "sl_client_order_id": "x-sl"}})
    c2 = _client(fetch_oco_status=AsyncMock(return_value={"listOrderStatus": "ALL_DONE"}),
                 fetch_order_by_client_id=AsyncMock(side_effect=lambda sym, cid: {"id": "TP", "status": "closed", "average": 62000.0, "filled": 0.1}
                                                    if cid == "x-tp" else {"id": "SL", "status": "canceled"}))
    assert run(cx.reconcile_protected_positions(db, lambda a: c2)) == 1
    assert t["status"] == "closed" and t["exit_price"] == 62000.0 and t["pnl"] == pytest.approx(200.0) and t["close_reason"] == "oco_tp"


def test_unknown_outcome_keeps_reservation_held_and_engine_inserts_once():
    import account_reservations as ar
    db = FakeDb()
    acc = {"_id": ObjectId()}

    async def unknown_stage(rid):
        return {"blocked": "exchange_unknown", "intent_id": "xin_1"}
    out = run(ar.guard_entry(db, account=acc, user_id="u", signal={"symbol": "BTCUSD", "action": "BUY", "signal_id": "u1"},
                             source="crypto", max_concurrent=3, cfg_account_id=str(acc["_id"]), run=unknown_stage))
    r = db.risk_reservations.rows[0]
    assert out["reservation_id"] == r["reservation_id"] and r["state"] == "RISK_RESERVED" and r["uncertain"] is True
    from crypto_bridge.binance_engine import BinanceCCXTEngine
    src = inspect.getsource(BinanceCCXTEngine._engine_stage)
    assert "cx.insert_trade_once(" in src and "db.trades.insert_one(" not in src and "reservation_id=reservation_id" in src
    assert "cx.filled_base_amount(" in src


# ── Q-1 netting volume on every leg · Q-2 installer re-run ───────────────────
def test_q1_live_volume_updates_every_open_leg_of_the_ticket():
    src = _src("backend", "routes", "bridge_routes.py")
    seg = src.split('"live_volume": float(p.volume)')[1][:600]
    assert 'db.trades.update_many({"account_id": account_id, "mt5_ticket": int(p.ticket),' in seg
    assert 'update_one({"_id": existing["_id"]}, {"$set": _live})' not in seg


def test_q2_second_installer_run_keeps_running_ea_grace_and_promotes_without_grace():
    import bridge_tokens as bt
    with patch.dict(os.environ, {"BRIDGE_TOKEN_HASH_KEY": "k" * 40, "JWT_SECRET": "j" * 40}):
        h0 = bt.token_hash("tok_running")
        h1 = bt.token_hash("tok_unused")
        future = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
        acc = {"_id": ObjectId(), "bridge_token_hash": h1, "bridge_token_prev_hash": h0, "bridge_token_prev_expires": future}
        later = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        upd = bt.rotation_update(acc, "tok_new", grace_until=later, suspended=False, keep_prev_grace=True)
        assert upd["$set"]["bridge_token_prev_hash"] == h0            # the RUNNING EA keeps its grace slot
        assert upd["$set"]["bridge_token_prev_expires"] == later
        assert upd["$addToSet"]["bridge_token_retired_hashes"] == h1  # the never-used token is retired
        plain = bt.rotation_update(acc, "tok_new", grace_until=later, suspended=False)
        assert plain["$set"]["bridge_token_prev_hash"] == h1
    setup = _src("backend", "routes", "setup_routes.py")
    assert "keep_prev_grace=_stale_pending" in setup and '_upd["$set"]["installation_pending"] = True' in setup
    bridge = _src("backend", "routes", "bridge_routes.py")
    assert 'elif acc.get("installation_pending"):' in bridge and "promote_pending_installation(db, acc)" in bridge
    # promotion with no grace slot: pending installation becomes authoritative, flag cleared
    db = FakeDb()
    acc_id = ObjectId()
    db.installations.rows.append({"_id": ObjectId(), "account_id": str(acc_id), "installation_id": "new", "pending_first_heartbeat": True})
    db.installations.rows.append({"_id": ObjectId(), "account_id": str(acc_id), "installation_id": "old"})
    db.accounts.rows.append({"_id": acc_id, "installation_pending": True})
    assert run(bt.promote_pending_installation(db, {"_id": acc_id})) == "new"
    assert "installation_pending" not in db.accounts.rows[0]
    assert [i for i in db.installations.rows if i["installation_id"] == "old"][0]["revoked"] is True


# ── N97-10 step-up · N97-11 attestation-based demo + every account on the card ──
def test_step_up_on_unlock_endpoints_and_attestation_based_demo():
    import acceptance_bundle as ab
    import routes.admin_routes as ar_
    for fn in (ar_.admin_acceptance_generate, ar_.admin_demo_readiness_manual):
        assert "require_step_up(" in inspect.getsource(fn), fn
    assert "attested_environment" in inspect.getsource(ab.account_evidence) and "demo_proof" not in inspect.getsource(ab)
    import trading_authority as ta
    assert "attested_environment" in inspect.getsource(ta.acceptance_domain)
    db = FakeDb()
    for i in range(2):
        db.accounts.rows.append({"_id": ObjectId(), "mode": "live", "trading_enabled": True, "label": f"L{i}", "status": "active"})
    with patch("broker_env.attested_environment", lambda a: "LIVE"), patch("modules.pamm.strategy_guard.GIT_COMMIT", "abc"):
        st = run(ab.current_status(db))
    assert len(st["accounts"]) == 2 and all(a["gate_applies"] and not a["covered"] for a in st["accounts"])
    assert st["valid_for_release"] is False
    card = _src("frontend", "src", "components", "AcceptanceBundleCard.jsx")
    assert "acceptance-bundle-accounts" in card and "s.accounts.map" in card


def test_ui_live_crypto_badge_and_empty_cap_field_keeps_server_default():
    badge = _src("frontend", "src", "components", "CryptoLiveBadge.jsx")
    assert "LIVE CRYPTO:" in badge and "/crypto/status" in badge
    assert "CryptoLiveBadge" in _src("frontend", "src", "pages", "DemoReadiness.jsx")
    assert "CryptoLiveBadge" in _src("frontend", "src", "pages", "Crypto.jsx")
    bot = _src("frontend", "src", "pages", "BotConfig.jsx")
    pp = bot.split("function PPNumInput(")[1].split("\n}\n")[0]
    # main98 — no snap-back while typing; the server default is applied on blur only
    assert "setDraft" in pp and 'parseFloat(e.target.value) || 0' not in pp and "[field]: fallback" in pp
