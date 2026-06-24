"""Execution Factory — routes a trade to the right engine based on account mode.

Paper accounts execute against the local DB (instant fill at live mid-price).
Live MT5 accounts hand the trade to the bridge queue for the EA to fulfil.
Future broker engines (Binance/CCXT) plug in here without touching call sites.
"""
import logging
from datetime import datetime, timezone
from abc import ABC, abstractmethod

from database import get_db
from market import get_quote
from ws_manager import manager as ws_manager

logger = logging.getLogger("execution")


class ExecutionEngine(ABC):
    @abstractmethod
    async def execute(self, *, user_id, account, signal,
                      max_concurrent: int = 0, cfg_account_id: str = None) -> dict: ...


class MT5BridgeEngine(ExecutionEngine):
    """Live engine — inserts a `pending` trade; the MT5 EA polls and executes."""

    async def execute(self, *, user_id, account, signal,
                      max_concurrent: int = 0, cfg_account_id: str = None) -> dict:
        db = get_db()
        # Atomic last-line-of-defense cap check. bot_runner.py reads `inflight`
        # ONCE per loop iteration — hot reloads can spawn duplicate loops that
        # all see stale counts. Re-counting right before insert closes the race.
        if max_concurrent > 0:
            cap_q = {"user_id": user_id, "status": {"$in": ["pending", "open"]}}
            if cfg_account_id:
                cap_q["account_id"] = cfg_account_id
            live_inflight = await db.trades.count_documents(cap_q)
            if live_inflight >= max_concurrent:
                logger.warning(
                    "execute blocked by max_concurrent cap user=%s acct=%s sym=%s "
                    "inflight=%d cap=%d",
                    user_id, cfg_account_id or "default",
                    signal.get("symbol"), live_inflight, max_concurrent,
                )
                return {"blocked": "max_concurrent_cap",
                        "inflight": live_inflight, "cap": max_concurrent}
        trade_doc = {
            "user_id": user_id,
            "account_id": str(account["_id"]),
            "signal_id": signal.get("signal_id"),
            "symbol": signal["symbol"],
            "action": signal["action"],
            "lot_size": signal["lot_size"],
            "original_lot_size": signal["lot_size"],
            "entry_price": signal["entry_price"],
            "stop_loss": signal["stop_loss"],
            "original_stop_loss": signal["stop_loss"],
            "take_profit": signal["take_profit"],
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
            "status": "pending",
            "mode": "live",
            "broker": account.get("broker", "MT5"),
            "mt5_ticket": None,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
            "error": None,
            "origin": signal.get("origin", "manual"),
            "partial_closed": False,
            "breakeven_set": False,
            "trail_active": False,
        }
        r = await db.trades.insert_one(trade_doc)
        trade_doc["id"] = str(r.inserted_id)
        trade_doc.pop("_id", None)
        await ws_manager.broadcast(user_id, "trade_created", trade_doc)
        return trade_doc


class PaperEngine(ExecutionEngine):
    """Virtual engine — simulates an instant fill at the current live mid-price."""

    async def execute(self, *, user_id, account, signal,
                      max_concurrent: int = 0, cfg_account_id: str = None) -> dict:
        db = get_db()
        if max_concurrent > 0:
            cap_q = {"user_id": user_id, "status": {"$in": ["pending", "open"]}}
            if cfg_account_id:
                cap_q["account_id"] = cfg_account_id
            live_inflight = await db.trades.count_documents(cap_q)
            if live_inflight >= max_concurrent:
                logger.warning(
                    "paper execute blocked by max_concurrent cap user=%s acct=%s "
                    "sym=%s inflight=%d cap=%d",
                    user_id, cfg_account_id or "default",
                    signal.get("symbol"), live_inflight, max_concurrent,
                )
                return {"blocked": "max_concurrent_cap",
                        "inflight": live_inflight, "cap": max_concurrent}
        quote = await get_quote(signal["symbol"])
        fill_price = quote.get("price") or signal["entry_price"]

        trade_doc = {
            "user_id": user_id,
            "account_id": str(account["_id"]),
            "signal_id": signal.get("signal_id"),
            "symbol": signal["symbol"],
            "action": signal["action"],
            "lot_size": signal["lot_size"],
            "entry_price": round(fill_price, 5),
            "stop_loss": signal["stop_loss"],
            "take_profit": signal["take_profit"],
            "tp1": signal.get("tp1"),
            "tp2": signal.get("tp2"),
            "tp3": signal.get("tp3"),
            "sl_pips": signal.get("sl_pips"),
            "tp_pips": signal.get("tp_pips"),
            "tp1_closed": False,
            "tp2_closed": False,
            "tp3_closed": False,
            "original_stop_loss": signal["stop_loss"],
            "original_lot_size": signal["lot_size"],
            "exit_price": None,
            "pnl": 0.0,
            "status": "open",
            "mode": "paper",
            "broker": "INTERNAL_PAPER",
            "mt5_ticket": None,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
            "error": None,
            "origin": signal.get("origin", "manual"),
        }
        r = await db.trades.insert_one(trade_doc)
        trade_doc["id"] = str(r.inserted_id)
        trade_doc.pop("_id", None)
        await ws_manager.broadcast(user_id, "trade_created", trade_doc)
        try:
            from notifier import notify_trade_opened
            sent = await notify_trade_opened(user_id, trade_doc)
            # Mark notified_opened=True so bridge_routes.py doesn't double-send
            # if the EA later reports the same trade as 'open'.
            if sent:
                await db.trades.update_one(
                    {"_id": r.inserted_id}, {"$set": {"notified_opened": True}}
                )
        except Exception:
            pass
        return trade_doc


def for_account(account: dict) -> ExecutionEngine:
    """Pick the right engine for an account."""
    mode = (account.get("mode") or "live").lower()
    if mode == "paper":
        return PaperEngine()
    return MT5BridgeEngine()


# ---------- Paper trade lifecycle ----------
async def settle_paper_trades_against_price() -> int:
    """Sweep all open paper trades; close any that have hit SL or TP.

    Returns number of trades closed in this sweep.
    """
    db = get_db()
    cursor = db.trades.find({"status": "open", "mode": "paper"})
    open_trades = await cursor.to_list(length=500)
    closed = 0
    # Cache quotes per symbol to avoid hammering free APIs
    quote_cache = {}
    for t in open_trades:
        sym = t["symbol"]
        if sym not in quote_cache:
            try:
                quote_cache[sym] = await get_quote(sym)
            except Exception:
                continue
        q = quote_cache[sym]
        price = q.get("price")
        if not price:
            continue
        action = t["action"]
        sl = t.get("stop_loss")
        tp = t.get("take_profit")
        hit_sl = (action == "BUY" and price <= sl) or (action == "SELL" and price >= sl)
        hit_tp = (action == "BUY" and price >= tp) or (action == "SELL" and price <= tp)
        if not (hit_sl or hit_tp):
            continue
        # Simulated P&L using lot_size as a generic unit multiplier (microcent convention)
        direction = 1 if action == "BUY" else -1
        pnl = round(direction * (price - t["entry_price"]) * t["lot_size"], 4)
        now_iso = datetime.now(timezone.utc).isoformat()
        await db.trades.update_one(
            {"_id": t["_id"]},
            {"$set": {
                "status": "closed",
                "exit_price": round(price, 5),
                "pnl": pnl,
                "closed_at": now_iso,
                "close_reason": "stop_loss" if hit_sl else "take_profit",
            }},
        )
        # Update virtual balance
        await db.accounts.update_one(
            {"_id": t["account_id_obj"]} if "account_id_obj" in t else
            {"_id": (await db.accounts.find_one({"_id": _to_oid(t["account_id"])}))["_id"]
             if t.get("account_id") else None},
            {"$inc": {"balance": pnl, "equity": pnl}},
        )
        await ws_manager.broadcast(t["user_id"], "trade_updated", {
            "trade_id": str(t["_id"]),
            "status": "closed",
            "exit_price": round(price, 5),
            "pnl": pnl,
            "close_reason": "stop_loss" if hit_sl else "take_profit",
        })
        try:
            from notifier import notify_trade_closed
            await notify_trade_closed(t["user_id"], {**t, "exit_price": round(price, 5), "pnl": pnl})
        except Exception:
            pass
        closed += 1
    return closed


def _to_oid(v):
    from bson import ObjectId
    try:
        return ObjectId(v)
    except Exception:
        return v
