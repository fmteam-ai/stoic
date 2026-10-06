"""Binance Spot execution engine — routes a STOIC signal to a real fill.

Mirrors `MT5BridgeEngine` so the bot_runner / signal pipeline doesn't care
which broker is on the other end:

  Signal → max_concurrent guard → audit_pre_trade (Safety Guardian) →
  Smart Router (Market vs Limit) → ccxt order → trade doc persisted →
  WebSocket broadcast → Telegram alert.

A few crypto-specific rules:
  • `lot_size` in the signal is interpreted as the **base-asset amount**
    (e.g. 0.001 = 0.001 BTC). No microcent multiplier — crypto has no
    fractional contract spec games to play.
  • The crypto safety floor is INDEPENDENT of MT5's pip-derived floor;
    we keep the same audit_pre_trade call but in addition apply a
    tighter per-trade cap (`CRYPTO_MAX_RISK_PCT_PER_TRADE`, default 0.5%).
  • A13 P0-01: a durable ExecutionIntent + deterministic client order id precede
    every exchange call; filled positions get exchange-side OCO protection or are
    flattened (fail closed). See crypto_bridge/crypto_execution.py.
  • Defence-in-depth: any account that hasn't been explicitly flipped to
    `live=true` AND `BINANCE_LIVE_ENABLED=true` runs against the sandbox.
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone

from database import get_db
from safety_guardian import audit_pre_trade
from ws_manager import manager as ws_manager
from execution import ExecutionEngine
from crypto_bridge.binance_ccxt import (
    BinanceClient, normalize_symbol, _is_testnet,
)

logger = logging.getLogger("crypto.execution")


def _crypto_risk_cap_pct() -> float:
    try:
        return float(os.environ.get("CRYPTO_MAX_RISK_PCT_PER_TRADE", "0.5"))
    except Exception:
        return 0.5


def _smart_route(signal: dict) -> tuple[str, float | None]:
    """Decide order type for a crypto signal.

    Returns (order_type, limit_price_or_None).
      - `signal.execution_hint == "limit"` and `signal.limit_price` set → LIMIT
      - Otherwise MARKET.
    """
    hint = (signal.get("execution_hint") or "").lower()
    if hint == "limit" and signal.get("limit_price"):
        return "limit", float(signal["limit_price"])
    return "market", None


class BinanceCCXTEngine(ExecutionEngine):
    """Live engine for Binance Spot accounts via CCXT."""

    async def execute(self, *, user_id, account, signal,
                      max_concurrent: int = 0, cfg_account_id: str = None) -> dict:
        # A13 P1-02 — account-wide entry reservation wraps the engine stage.
        from account_reservations import guard_entry

        async def _stage(reservation_id):
            return await self._engine_stage(user_id=user_id, account=account, signal=signal,
                                            cfg_account_id=cfg_account_id, reservation_id=reservation_id)
        return await guard_entry(get_db(), account=account, user_id=user_id, signal=signal,
                                 source="crypto", max_concurrent=max_concurrent,
                                 cfg_account_id=cfg_account_id, run=_stage)

    async def _engine_stage(self, *, user_id, account, signal, cfg_account_id, reservation_id) -> dict:
        db = get_db()
        # N97-6 — a "testnet" account on an exchange without a sandbox would trade REAL money
        # while every live gate sees it as testnet: refuse before anything else.
        from crypto_bridge.ccxt_engine import sandbox_available
        if account.get("testnet") and not sandbox_available(account):
            return {"blocked": "testnet_unavailable",
                    "reason": f"{account.get('exchange_id') or 'exchange'} has no sandbox — a testnet account "
                              "cannot trade here (would hit live endpoints). Remove it or connect it as LIVE."}
        # N98-10 — spot has no short side: a SELL "entry" would sell the user's other coins. Refuse.
        if str(signal.get("action", "")).upper() == "SELL" and not account.get("futures"):
            return {"blocked": "spot_sell_entry_refused",
                    "reason": "spot accounts cannot open SELL positions — only BUY entries are accepted"}
        # A13-1 (P0-01 step 1) — operator kill switch: with live crypto OFF every non-testnet
        # order is refused HERE, before any exchange call. Default off.
        from crypto_bridge.ccxt_engine import _live_enabled, _wants_live, live_capability_block
        if _wants_live(account):
            cap_block = live_capability_block(account.get("exchange_id") or "binance")   # A14-5
            if cap_block:
                return cap_block
        if _wants_live(account) and not _live_enabled():
            logger.warning("binance execute refused: live crypto trading disabled user=%s sym=%s",
                           user_id, signal.get("symbol"))
            return {"blocked": "crypto_live_disabled",
                    "reason": "Live crypto trading is switched off (CRYPTO_LIVE_TRADING_ENABLED / BINANCE_LIVE_ENABLED)."}
        # round 9 P0-01 — canonical trading decision: crypto is not a bypass.
        from trading_authority import gate_or_block
        _deny = await gate_or_block(db, account, "binance_engine")
        if _deny:
            return _deny

        # FINAL ENTITLEMENT CHECK (iter-122 Phase 2) — same authority
        # boundary as the MT5 engine; crypto is not a bypass.
        from entitlements import verify_execution_entitlement
        ent_block = await verify_execution_entitlement(
            db, user_id=user_id, account=account, signal=signal)
        if ent_block:
            logger.warning("binance execute blocked by entitlement user=%s sym=%s: %s",
                           user_id, signal.get("symbol"), ent_block.get("reason"))
            return ent_block

        symbol_internal = signal["symbol"]
        ccxt_symbol = normalize_symbol(symbol_internal)
        action = (signal.get("action") or "").upper()
        side = "buy" if action == "BUY" else "sell"
        amount = float(signal.get("lot_size") or 0)

        # 1. Concurrency / daily caps were enforced by the account-wide reservation (A13 P1-02).

        # 2. Safety Guardian (same audit as MT5 — equity, daily-loss, etc.).
        safety = await audit_pre_trade(
            db=db, account=account, signal=signal,
            user_id=user_id, cfg_account_id=cfg_account_id,
        )
        if not safety["ok"]:
            logger.warning(
                "SAFETY GUARDIAN refused crypto trade user=%s sym=%s blocked_by=%s",
                user_id, ccxt_symbol, safety.get("blocked_by"),
            )
            try:
                await db.safety_blocks.insert_one({
                    "user_id": user_id,
                    "account_id": cfg_account_id,
                    "symbol": symbol_internal,
                    "action": action,
                    "lot_size": amount,
                    "blocked_by": safety["blocked_by"],
                    "audit": safety["audit"],
                    "context": safety.get("context"),
                    "broker": "BINANCE_SPOT",
                    "blocked_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to persist safety_block: %s", e)
            return {"blocked": "safety_guardian",
                    "safety_blocked_by": safety["blocked_by"],
                    "safety_audit": safety["audit"]}

        # 3. Crypto-specific tighter cap (% of equity in $ at entry).
        # Per-account override (bot_config.crypto_risk_pct_per_trade) takes
        # precedence over the global env CRYPTO_MAX_RISK_PCT_PER_TRADE.
        per_account_cap = None
        if cfg_account_id:
            try:
                cfg_doc = await db.bot_configs.find_one({
                    "user_id": user_id, "account_id": cfg_account_id,
                })
                if cfg_doc and cfg_doc.get("crypto_risk_pct_per_trade") is not None:
                    per_account_cap = float(cfg_doc["crypto_risk_pct_per_trade"])
            except Exception:
                per_account_cap = None
        cap_pct_used = per_account_cap if per_account_cap is not None else _crypto_risk_cap_pct()

        equity = float(account.get("equity") or account.get("balance") or 0)
        entry_px = float(signal.get("entry_price") or 0)
        sl_px = float(signal.get("stop_loss") or 0)
        risk_per_unit = abs(entry_px - sl_px) if entry_px > 0 and sl_px > 0 else 0
        crypto_risk_usd = risk_per_unit * amount
        crypto_cap = equity * (cap_pct_used / 100.0)
        if equity > 0 and crypto_risk_usd > crypto_cap and crypto_cap > 0:
            logger.warning(
                "BINANCE per-trade risk cap exceeded user=%s risk=$%.2f cap=$%.2f (%.2f%%)",
                user_id, crypto_risk_usd, crypto_cap, cap_pct_used,
            )
            return {"blocked": "crypto_risk_cap",
                    "risk_usd": crypto_risk_usd, "cap_usd": crypto_cap,
                    "cap_pct_used": cap_pct_used,
                    "cap_source": "per_account" if per_account_cap is not None else "env_default"}

        # 4. A13 P0-01 — durable intent + deterministic client order id BEFORE the
        #    exchange call; failures classify to rejected / UNKNOWN (never resent).
        from crypto_bridge import crypto_execution as cx
        order_type, limit_price = _smart_route(signal)
        intent = await cx.begin(db, account_id=str(account["_id"]), user_id=user_id, signal=signal,
                                order_type=order_type, ccxt_symbol=ccxt_symbol, side=side, amount=amount,
                                reservation_id=reservation_id)
        if intent.get("blocked"):
            return intent
        cid = intent["client_order_id"]
        order_resp: dict = {}
        client = BinanceClient(account)
        try:
            await client.__aenter__()
            if order_type == "limit":
                order_resp = await client.create_limit_order(ccxt_symbol, side, amount, limit_price,
                                                             client_order_id=cid)
            else:
                order_resp = await client.create_market_order(ccxt_symbol, side, amount, client_order_id=cid)
        except Exception as e:  # noqa: BLE001
            logger.exception("Binance order placement failed: %s", e)
            await client.__aexit__(None, None, None)
            return await cx.record_failure(db, intent["intent_id"], e)
        intent_state = await cx.record_outcome(db, intent["intent_id"], order_resp or {})
        if intent_state == "rejected":
            await client.__aexit__(None, None, None)
            return {"blocked": "exchange_rejected", "intent_id": intent["intent_id"],
                    "exchange_order_status": (order_resp or {}).get("status")}

        # 5. Persist trade doc.
        fill_price = (
            order_resp.get("average")
            or order_resp.get("price")
            or entry_px
            or 0.0
        )
        trade_doc = {
            "user_id": user_id,
            "account_id": str(account["_id"]),
            "signal_id": signal.get("signal_id"),
            "symbol": symbol_internal,
            "exchange_symbol": ccxt_symbol,
            "action": action,
            "lot_size": amount,
            "original_lot_size": amount,
            "entry_price": round(float(fill_price), 5),
            "stop_loss": sl_px or None,
            "original_stop_loss": sl_px or None,
            "take_profit": float(signal.get("take_profit") or 0) or None,
            "tp1": signal.get("tp1"),
            "tp2": signal.get("tp2"),
            "tp3": signal.get("tp3"),
            "sl_pips": signal.get("sl_pips"),
            "tp_pips": signal.get("tp_pips"),
            "tp1_closed": False,
            "tp2_closed": False,
            "tp3_closed": False,
            "exit_price": None,
            "pnl": 0.0,
            # Order types: market → 'open' immediately; limit → 'pending'.
            "status": "open" if intent_state == "filled" else "pending",
            "execution_intent_id": intent["intent_id"],
            "client_order_id": cid,
            "reservation_id": reservation_id,
            "protection": {"status": cx.PROTECTION_MISSING, "reason": "not yet placed"},
            "mode": "live" if not _is_testnet(account) else "paper",
            "broker": "BINANCE_SPOT",
            "broker_kind": "binance",
            "testnet": _is_testnet(account),
            "mt5_ticket": None,
            "exchange_order_id": str(order_resp.get("id") or ""),
            "exchange_order_status": order_resp.get("status"),
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
            "error": None,
            "origin": signal.get("origin", "manual"),
            "source": "binance",
            "broker_identity": {
                "exchange_id": account.get("exchange_id") or "binance",
                "api_key_fingerprint": account.get("api_key_fingerprint"),
                "account_number": account.get("account_number"),
            },
            "partial_closed": False,
            "breakeven_set": False,
            "trail_active": False,
            "safety_audit": safety,
            "crypto_risk_cap": {
                "risk_usd": round(crypto_risk_usd, 4),
                "cap_usd": round(crypto_cap, 4),
                "applied_pct": cap_pct_used,
                "cap_source": "per_account" if per_account_cap is not None else "env_default",
            },
        }

        try:
            from trade_explainer import snapshot_for_trade_creation
            trade_doc["explanation_snapshot"] = await snapshot_for_trade_creation(
                db, user_id=user_id, symbol=symbol_internal,
                signal=signal, trade_doc=trade_doc,
            )
        except Exception:  # noqa: BLE001
            pass

        # N97-8 — protect / flatten what we actually HOLD (filled minus base-asset fees)
        # A14-4 — ANY filled quantity is exposure: a resting partially filled order carries its held amount
        held = cx.filled_base_amount(order_resp, ccxt_symbol.split("/")[0]) if float(order_resp.get("filled") or 0) > 0 else 0.0
        trade_doc["held_amount"] = held
        if intent_state != "filled" and held > 0:
            trade_doc["partial_fill"] = True       # pending + exposure → the protection sweep cancels the remainder, then protects
        inserted_id, _new_row = await cx.insert_trade_once(db, trade_doc)      # N97-9 — sweep race safe
        await cx.mark_trade_recorded(db, intent["intent_id"], inserted_id)
        trade_doc["id"] = str(inserted_id)
        trade_doc.pop("_id", None)
        # 6. Exchange-side protection for filled positions (fail closed → flatten + alert).
        try:
            if intent_state == "filled":
                trade_doc["protection"] = await cx.protect(
                    db, client, trade_id=inserted_id, ccxt_symbol=ccxt_symbol, side=side, amount=held,
                    stop_loss=sl_px or None, take_profit=trade_doc["take_profit"], cid=cid, account=account)
                if trade_doc["protection"]["status"] == cx.PROTECTION_FLATTENED:
                    trade_doc["status"] = "closed"
        finally:
            await client.__aexit__(None, None, None)
        await ws_manager.broadcast(user_id, "trade_created", trade_doc)

        try:
            from notifier import notify_trade_opened
            sent = await notify_trade_opened(user_id, trade_doc)
            if sent:
                await db.trades.update_one(
                    {"_id": inserted_id}, {"$set": {"notified_opened": True}},
                )
        except Exception:  # noqa: BLE001
            pass

        return trade_doc
