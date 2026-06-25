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
  • SL/TP are stamped on the trade doc but NOT placed as OCO orders in
    v1 — those will land in iter-36 (Profitability Pack phase 2).
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
        db = get_db()
        symbol_internal = signal["symbol"]
        ccxt_symbol = normalize_symbol(symbol_internal)
        action = (signal.get("action") or "").upper()
        side = "buy" if action == "BUY" else "sell"
        amount = float(signal.get("lot_size") or 0)

        # 1. Max-concurrent guard (same pattern as MT5BridgeEngine).
        if max_concurrent > 0:
            cap_q = {"user_id": user_id, "status": {"$in": ["pending", "open"]}}
            if cfg_account_id:
                cap_q["account_id"] = cfg_account_id
            live_inflight = await db.trades.count_documents(cap_q)
            if live_inflight >= max_concurrent:
                logger.warning(
                    "binance execute blocked by max_concurrent user=%s "
                    "sym=%s inflight=%d cap=%d",
                    user_id, ccxt_symbol, live_inflight, max_concurrent,
                )
                return {"blocked": "max_concurrent_cap",
                        "inflight": live_inflight, "cap": max_concurrent}

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
        equity = float(account.get("equity") or account.get("balance") or 0)
        entry_px = float(signal.get("entry_price") or 0)
        sl_px = float(signal.get("stop_loss") or 0)
        risk_per_unit = abs(entry_px - sl_px) if entry_px > 0 and sl_px > 0 else 0
        crypto_risk_usd = risk_per_unit * amount
        crypto_cap = equity * (_crypto_risk_cap_pct() / 100.0)
        if equity > 0 and crypto_risk_usd > crypto_cap and crypto_cap > 0:
            logger.warning(
                "BINANCE per-trade risk cap exceeded user=%s risk=$%.2f cap=$%.2f",
                user_id, crypto_risk_usd, crypto_cap,
            )
            return {"blocked": "crypto_risk_cap",
                    "risk_usd": crypto_risk_usd, "cap_usd": crypto_cap}

        # 4. Smart-routed order.
        order_type, limit_price = _smart_route(signal)
        order_resp: dict = {}
        try:
            async with BinanceClient(account) as client:
                if order_type == "limit":
                    order_resp = await client.create_limit_order(
                        ccxt_symbol, side, amount, limit_price,
                    )
                else:
                    order_resp = await client.create_market_order(
                        ccxt_symbol, side, amount,
                    )
        except Exception as e:  # noqa: BLE001
            logger.exception("Binance order placement failed: %s", e)
            return {"blocked": "exchange_error",
                    "error": str(e)[:200]}

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
            "status": "open" if order_type == "market" else "pending",
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
            "partial_closed": False,
            "breakeven_set": False,
            "trail_active": False,
            "safety_audit": safety,
            "crypto_risk_cap": {
                "risk_usd": round(crypto_risk_usd, 4),
                "cap_usd": round(crypto_cap, 4),
                "applied_pct": _crypto_risk_cap_pct(),
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

        r = await db.trades.insert_one(trade_doc)
        trade_doc["id"] = str(r.inserted_id)
        trade_doc.pop("_id", None)
        await ws_manager.broadcast(user_id, "trade_created", trade_doc)

        try:
            from notifier import notify_trade_opened
            sent = await notify_trade_opened(user_id, trade_doc)
            if sent:
                await db.trades.update_one(
                    {"_id": r.inserted_id}, {"$set": {"notified_opened": True}},
                )
        except Exception:  # noqa: BLE001
            pass

        return trade_doc
