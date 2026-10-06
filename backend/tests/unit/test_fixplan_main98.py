"""main98 review — Part 1 (before the demo) + N98-3/4/5/7/8/9/10 + Q-2 slide + audit rows (pure unit)."""
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


class _IndexedCollection(FakeCollection):
    """Mongo-like index bookkeeping: same key + different options ⇒ IndexOptionsConflict (85)."""
    def __init__(self):
        super().__init__()
        self.indexes = {"_id_": {"key": [("_id", 1)]}}
        self.dropped = []

    async def index_information(self):
        return dict(self.indexes)

    async def drop_index(self, name):
        self.dropped.append(name)
        self.indexes.pop(name)

    async def create_index(self, key, **opts):
        from pymongo.errors import OperationFailure
        name = opts.get("name") or f"{key}_1"
        for n, spec in self.indexes.items():
            if [k for k, _ in spec["key"]] == [key] and n != name:
                if spec.get("opts") != {k: v for k, v in opts.items() if k != "name"}:
                    raise OperationFailure("Index with name: %s already exists with different options" % n, code=85)
        self.indexes[name] = {"key": [(key, 1)], "opts": {k: v for k, v in opts.items() if k != "name"}}
        return name


# ── Part 1: .env templates + secrets ─────────────────────────────────────────
def test_env_templates_and_docker_secret_carry_the_keys():
    be = _src("deploy", "env", "backend.env.example")
    for k in ("BRIDGE_TOKEN_HASH_KEY=", "CRYPTO_LIVE_TRADING_ENABLED=", "LEDGER_ANCHOR_KEY="):
        assert k in be, k
    root = _src("deploy", "env", "root.env.example")
    for k in ("BRIDGE_TOKEN_HASH_KEY=", "CRYPTO_LIVE_TRADING_ENABLED=", "LEDGER_ANCHOR_KEY="):
        assert k in root, k
    inst = _src("deploy", "install.sh").split("cat > backend/.env.example <<'EOF'")[1].split("\nEOF")[0]
    assert "BRIDGE_TOKEN_HASH_KEY=" in inst and "CRYPTO_LIVE_TRADING_ENABLED=" in inst
    compose = _src("docker-compose.yml")
    assert "BRIDGE_TOKEN_HASH_KEY_FILE: /run/secrets/bridge_token_hash_key" in compose
    assert "bridge_token_hash_key:\n    file: ./secrets/bridge_token_hash_key" in compose
    lib = _src("deploy", "lib.sh")
    assert "for f in order_auth_secret ledger_anchor_key bridge_token_hash_key; do" in lib
    upd = _src("deploy", "update.sh")
    assert "secrets/bridge_token_hash_key" in upd and "production requires BRIDGE_TOKEN_HASH_KEY" in upd


# ── N98-1 TTL index conflict ─────────────────────────────────────────────────
def test_lock_index_upgrade_from_main97_database_does_not_conflict():
    import account_reservations as ar
    db = FakeDb()
    col = _IndexedCollection()
    db.account_reservation_locks = col
    # a main97 database: plain locked_until_1 already exists
    run(col.create_index("locked_until"))
    assert "locked_until_1" in col.indexes
    run(ar.ensure_reservation_lock_indexes(db))            # must NOT raise OperationFailure 85
    assert col.dropped == ["locked_until_1"]
    assert col.indexes[ar.LOCK_TTL_INDEX]["opts"] == {"expireAfterSeconds": 300}
    run(ar.ensure_reservation_lock_indexes(db))            # idempotent on a main98 database
    assert col.dropped == ["locked_until_1"] and ar.LOCK_TTL_INDEX in col.indexes
    # a fresh database
    db2 = FakeDb()
    db2.account_reservation_locks = _IndexedCollection()
    run(ar.ensure_reservation_lock_indexes(db2))
    assert ar.LOCK_TTL_INDEX in db2.account_reservation_locks.indexes


# ── N98-2 cap resolution = bot_runner's ──────────────────────────────────────
def test_daily_cap_matches_bot_runner_resolution_no_silent_fallback():
    import account_reservations as ar
    db = FakeDb()
    aid = str(ObjectId())
    # no per-account config → the user's DEFAULT profile decides (preset says 4)
    db.bot_configs.rows.append({"user_id": "u", "account_id": None, "trade_of_day_cap": 4, "max_concurrent_trades": 5})
    caps = run(ar.caps_for(db, user_id="u", cfg_account_id=aid, max_concurrent=None))
    assert caps["daily_cap"] == 4 and caps["auto_cap"] == 5
    # per-account config wins when present
    db.bot_configs.rows.append({"user_id": "u", "account_id": aid, "trade_of_day_cap": 2, "max_concurrent_trades": 3})
    caps2 = run(ar.caps_for(db, user_id="u", cfg_account_id=aid, max_concurrent=None))
    assert caps2["daily_cap"] == 2 and caps2["auto_cap"] == 3
    # bot_runner passes the cap it already resolved — the guard uses exactly that value
    caps3 = run(ar.caps_for(db, user_id="u", cfg_account_id=aid, max_concurrent=3, daily_cap=7))
    assert caps3["daily_cap"] == 7
    assert ar.resolve_daily_cap({"trade_of_day_cap": ""}) == 1 and ar.resolve_daily_cap({"trade_of_day_cap": 0}) == 0
    assert '"_daily_cap": trade_of_day_cap' in _src("backend", "bot_runner.py")
    assert 'daily_cap=signal.get("_daily_cap")' in inspect.getsource(ar.guard_entry)


# ── N98-3 scalp has its own counter ──────────────────────────────────────────
def test_scalp_uses_its_own_origin_and_caps_not_the_main_bots():
    import account_reservations as ar
    db = FakeDb()
    aid = str(ObjectId())
    today = datetime.now(timezone.utc).isoformat()
    db.bot_configs.rows.append({"user_id": "u", "account_id": aid, "trade_of_day_cap": 1, "max_concurrent_trades": 1})
    db.trades.rows.append({"user_id": "u", "account_id": aid, "status": "open", "origin": "auto", "symbol": "XAUUSD", "opened_at": today})
    main = run(ar.caps_for(db, user_id="u", cfg_account_id=aid, max_concurrent=None))
    r_main = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="mt5", decision_id="m", caps=main, symbol="XAUUSD", origin="auto"))
    assert r_main["ok"] is False                                 # main bot is at its cap (1 open)
    scalp = run(ar.caps_for(db, user_id="u", cfg_account_id=aid, max_concurrent=None, origin="scalp"))
    assert scalp["origin"] == "scalp" and scalp["auto_cap"] == 3 and scalp["daily_cap"] == 0
    r_scalp = run(ar.reserve_entry(db, account_id=aid, user_id="u", source="scalp", decision_id="s", caps=scalp, symbol="XAUUSD", origin="scalp"))
    assert r_scalp["ok"] is True and r_scalp["capacity"]["auto_open"] == 0   # the main bot's open trade is NOT its counter
    eng = _src("backend", "scalp", "engine.py")
    # N99-1 — the counter key stays "scalp" (reservation side) but the TRADE keeps origin "auto"
    # + engine "scalp" so every auto safety filter still applies
    assert 'origin="scalp"' in eng and '"origin": "auto", "engine": "scalp", "scope": "scalp_fast"' in eng
    assert scalp["total_cap"] == 3                                 # N99-1 — shares the account-wide total cap


# ── N98-4 suspension survives the re-hash ────────────────────────────────────
def test_suspended_token_stays_suspended_across_hash_variants():
    import bridge_tokens as bt
    env = {"JWT_SECRET": "j" * 40, "BRIDGE_TOKEN_HASH_KEY": ""}
    with patch.dict(os.environ, env):
        old = bt.token_hash("tok_s")
    with patch.dict(os.environ, {**env, "BRIDGE_TOKEN_HASH_KEY": "k" * 40}):
        acc = {"bridge_token_suspended": {"token_hash": old}}
        assert bt.is_suspended(acc, "tok_s") is True
        assert bt.is_suspended(acc, "tok_other") is False


# ── N98-5 Q-1 lookup on OPEN legs ────────────────────────────────────────────
def test_q1_lookup_prefers_the_open_leg():
    src = _src("backend", "routes", "bridge_routes.py")
    seg = src.split("# N98-5")[1][:700]
    assert '"mt5_ticket": int(p.ticket), "status": "open",' in seg and '"status": {"$ne": "open"}' in seg


# ── N98-7 update.sh stops the stack in the restore-failure branch ────────────
def test_update_sh_stops_stack_before_declaring_it_stopped():
    sh = _src("deploy", "update.sh")
    block = sh.split('if ! RESTORE_NO_START=1')[1].split("fi")[0]
    assert "docker compose stop" in block and block.index("docker compose stop") < block.index("stack left STOPPED")


# ── Q-2 abandoned installer re-run never cuts the old EA off ─────────────────
def test_abandoned_rerun_slides_grace_while_new_token_unused():
    import bridge_tokens as bt
    db = FakeDb()
    acc_id = ObjectId()
    soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    db.accounts.rows.append({"_id": acc_id, "installation_pending": True, "bridge_token_prev_expires": soon})
    run(bt.extend_prev_grace(db, db.accounts.rows[0]))
    assert db.accounts.rows[0]["bridge_token_prev_expires"] > (datetime.now(timezone.utc) + timedelta(hours=23)).isoformat()
    # not pending (new token already used) → nothing slides
    db.accounts.rows.append({"_id": ObjectId(), "bridge_token_prev_expires": soon})
    run(bt.extend_prev_grace(db, db.accounts.rows[1]))
    assert db.accounts.rows[1]["bridge_token_prev_expires"] == soon
    assert "extend_prev_grace(db, acc)" in _src("backend", "routes", "bridge_routes.py")


def test_audit_rows_record_step_up_verified():
    assert '"step_up_verified": True' in _src("backend", "demo_readiness.py")
    ar_src = _src("backend", "routes", "admin_routes.py")
    assert '"step_up_verified": True' in ar_src.split("acceptance_bundle_generated")[1][:300]


# ── crypto N98-8 / N98-9 / N98-10 ────────────────────────────────────────────
def test_fresh_client_loads_markets_and_oco_uses_current_endpoint():
    from crypto_bridge.ccxt_engine import CCXTClient
    assert "await self.exchange.load_markets()" in inspect.getsource(CCXTClient.__aenter__)
    src = inspect.getsource(CCXTClient.place_oco_protection)
    assert "privatePostOrderlistOco" in src and "privatePostOrderOco(" not in src
    assert "aboveType" in src.replace('f"above{k}"', "aboveType") and "LIMIT_MAKER" in src and "STOP_LOSS_LIMIT" in src


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


def test_oco_lookup_before_resend_timeout_is_uncertain_and_adopts_live_list():
    import ccxt
    from crypto_bridge import crypto_execution as cx
    db = FakeDb()
    tid = ObjectId()
    row = {"_id": tid, "status": "open", "broker_kind": "binance", "account_id": "a", "protection": {"status": "MISSING"}}
    db.trades.rows.append(row)
    # OCO request times out → UNCERTAIN (lease kept), NOT flattened
    c = _client(place_oco_protection=AsyncMock(side_effect=ccxt.RequestTimeout("t/o")))
    prot = run(cx.protect(db, c, trade_id=tid, ccxt_symbol="BTC/USDT", side="buy", amount=0.1, stop_loss=1, take_profit=2,
                          cid="stoic-x", account={}))
    assert prot["status"] == cx.PROTECTION_PLACING and prot["uncertain"] is True
    assert c.create_market_order.await_count == 0
    # next sweep: the list IS live at the exchange → adopted, never re-placed
    c2 = _client(fetch_oco_status=AsyncMock(return_value={"orderListId": 77, "listOrderStatus": "EXECUTING"}))
    prot2 = run(cx.protect(db, c2, trade_id=tid, ccxt_symbol="BTC/USDT", side="buy", amount=0.1, stop_loss=1, take_profit=2,
                           cid="stoic-x", account={}))
    assert prot2["status"] == cx.PROTECTION_PLACED and prot2["adopted"] is True and prot2["list_id"] == "77"
    assert c2.place_oco_protection.await_count == 0
    # flatten path: a prior flatten order by client id is adopted, never resent
    row["protection"] = {"status": "MISSING"}
    c3 = _client(place_oco_protection=AsyncMock(side_effect=RuntimeError("oco unsupported")),
                 fetch_order_by_client_id=AsyncMock(return_value={"id": "OLDFL", "status": "closed", "average": 59990.0, "filled": 0.1}))
    prot3 = run(cx.protect(db, c3, trade_id=tid, ccxt_symbol="BTC/USDT", side="buy", amount=0.1, stop_loss=1, take_profit=2,
                           cid="stoic-y", account={}))
    assert prot3["status"] == cx.PROTECTION_FLATTENED and prot3["adopted"] is True and c3.create_market_order.await_count == 0


def test_partial_fill_is_a_position_setup_errors_reject_and_spot_sell_refused():
    from crypto_bridge import crypto_execution as cx
    from crypto_bridge.ccxt_engine import SandboxUnavailable
    assert cx.outcome_state({"status": "canceled", "filled": 0.02}) == "filled"
    assert cx.outcome_state({"status": "canceled", "filled": 0}) == "rejected"
    assert cx.never_left_exchange(SandboxUnavailable("no sandbox")) is True
    assert cx.never_left_exchange(ValueError("bad precision")) is True
    assert cx.never_left_exchange(TimeoutError()) is False
    it = {"intent_id": "xin_1", "client_order_id": "stoic-1", "actor": "u", "account_id": "a",
          "payload": {"exchange_symbol": "BTC/USDT", "side": "buy", "amount": 0.1, "symbol": "BTCUSD"}}
    rec = cx.trade_from_exchange_order(it, {"id": "E", "status": "canceled", "filled": 0.04, "average": 60000.0,
                                            "fees": [{"currency": "BTC", "cost": 0.00004}]})
    assert rec["status"] == "open" and rec["held_amount"] == pytest.approx(0.03996)      # fee-aware recovered size
    from crypto_bridge.binance_engine import BinanceCCXTEngine
    src = inspect.getsource(BinanceCCXTEngine._engine_stage)
    assert '"spot_sell_entry_refused"' in src and src.index("spot_sell_entry_refused") < src.index("cx.begin(")


def test_oco_without_fill_reopens_protection_and_uncertain_reservations_survive_sweep():
    from crypto_bridge import crypto_execution as cx
    from scalp import risk_reservations as rr
    db = FakeDb()
    acc_id = ObjectId()
    db.accounts.rows.append({"_id": acc_id})
    db.trades.rows.append({"_id": ObjectId(), "status": "open", "broker_kind": "binance", "account_id": str(acc_id),
                           "exchange_symbol": "BTC/USDT", "entry_price": 60000.0, "held_amount": 0.1,
                           "protection": {"status": cx.PROTECTION_PLACED, "list_client_order_id": "x-oco",
                                          "tp_client_order_id": "x-tp", "sl_client_order_id": "x-sl"}})
    c = _client(fetch_oco_status=AsyncMock(return_value={"listOrderStatus": "ALL_DONE"}),
                fetch_order_by_client_id=AsyncMock(return_value={"id": "L", "status": "canceled", "filled": 0}))
    assert run(cx.reconcile_protected_positions(db, lambda a: c)) == 0
    assert db.trades.rows[0]["protection"]["status"] == cx.PROTECTION_MISSING and db.trades.rows[0]["status"] == "open"
    old = (datetime.now(timezone.utc) - timedelta(seconds=rr.STALE_TTL_SEC + 60))
    db.risk_reservations.rows.append({"reservation_id": "r1", "state": "RISK_RESERVED", "uncertain": True, "updated_at": old,
                                      "account_id": "a", "transition_keys": [], "transitions": []})
    db.risk_reservations.rows.append({"reservation_id": "r2", "state": "RISK_RESERVED", "updated_at": old,
                                      "account_id": "a", "transition_keys": [], "transitions": []})
    out = run(rr.sweep_stale(db))
    assert out == {"released": 1, "kept": 1}
    assert db.risk_reservations.rows[0]["state"] == "RISK_RESERVED" and db.risk_reservations.rows[1]["state"] == "RELEASED"


def test_cap_field_keeps_empty_text_while_typing():
    bot = _src("frontend", "src", "pages", "BotConfig.jsx")
    pp = bot.split("function PPNumInput(")[1].split("\n}\n")[0]
    assert "const [draft, setDraft] = useState(null);" in pp and "setDraft(txt)" in pp
    assert 'onBlur={e => { if (e.target.value === "") setCfg({ ...cfg, [field]: fallback }); setDraft(null); }}' in pp
    assert "Number.isFinite(v) ? v : fallback" not in pp                 # no snap-back on change
