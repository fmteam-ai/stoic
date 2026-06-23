from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException
from bson import ObjectId
from pydantic import BaseModel

from database import get_db
from models import BridgeHeartbeat, BridgeTradeReport
from ws_manager import manager as ws_manager
from pip_utils import price_to_pips
from intelligence_counters import increment as inc_intel_counter
from trade_reconciler import reconcile_account, reconcile_user

router = APIRouter(prefix="/bridge", tags=["bridge"])


async def _account_by_token(token: str) -> dict:
    db = get_db()
    acc = await db.accounts.find_one({"bridge_token": token})
    if not acc:
        raise HTTPException(status_code=401, detail="Invalid bridge token")
    return acc


@router.post("/heartbeat")
async def heartbeat(payload: BridgeHeartbeat):
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    now_iso = datetime.now(timezone.utc).isoformat()
    set_doc = {
        "balance": payload.balance,
        "equity": payload.equity,
        "open_positions": payload.open_positions,
        "status": "connected",
        "last_heartbeat": now_iso,
    }
    if payload.spreads:
        # Normalise keys + clamp to non-negative floats
        clean = {}
        for sym, sp in payload.spreads.items():
            try:
                clean[str(sym).upper()] = max(0.0, float(sp))
            except Exception:
                continue
        if clean:
            set_doc["current_spreads"] = clean
            set_doc["spreads_updated_at"] = now_iso

    # EA v1.22+: persist the ticket list so the user can later trigger
    # manual reconciliation even if a heartbeat isn't currently in flight.
    reconcile_summary = None
    if payload.open_tickets is not None:
        try:
            tickets = [int(t) for t in payload.open_tickets if t is not None]
        except (TypeError, ValueError):
            tickets = []
        set_doc["open_tickets"] = tickets
        set_doc["open_tickets_updated_at"] = now_iso
        # Auto-reconcile on every heartbeat — closes orphans within ~5s of EA tick.
        reconcile_summary = await reconcile_account(
            str(acc["_id"]), tickets, source="heartbeat",
        )

    await db.accounts.update_one({"_id": acc["_id"]}, {"$set": set_doc})
    await ws_manager.broadcast(acc["user_id"], "account_heartbeat", {
        "account_id": str(acc["_id"]),
        "balance": payload.balance,
        "equity": payload.equity,
        "open_positions": payload.open_positions,
        "last_heartbeat": now_iso,
        "spreads": set_doc.get("current_spreads"),
    })
    resp = {"ok": True, "server_time": now_iso}
    if reconcile_summary and reconcile_summary["closed_count"] > 0:
        resp["reconciled"] = reconcile_summary
    return resp


class PollRequest(BaseModel):
    bridge_token: str


@router.post("/poll-trades")
async def poll_trades(payload: PollRequest):
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)

    # 1. Pending NEW trades (status='pending')
    new_cursor = db.trades.find({"account_id": str(acc["_id"]), "status": "pending"})
    pending = await new_cursor.to_list(length=20)
    out = []
    for t in pending:
        out.append({
            "trade_id": str(t["_id"]),
            "symbol": t["symbol"],
            "action": t["action"],
            "lot_size": t["lot_size"],
            "entry_price": t["entry_price"],
            "stop_loss": t["stop_loss"],
            "take_profit": t["take_profit"],
            "close_requested": t.get("close_requested", False),
            "mt5_ticket": t.get("mt5_ticket"),
        })

    # 2. Open trades with pending modifications (break-even / partial-close / trailing)
    mod_cursor = db.trades.find({
        "account_id": str(acc["_id"]),
        "status": "open",
        "mt5_ticket": {"$ne": None},
        "pending_modification": {"$exists": True, "$ne": None},
    })
    mods = await mod_cursor.to_list(length=20)
    modifications = []
    for t in mods:
        m = t.get("pending_modification") or {}
        modifications.append({
            "trade_id": str(t["_id"]),
            "mt5_ticket": t.get("mt5_ticket"),
            "symbol": t["symbol"],
            "type": m.get("type"),
            "new_sl": m.get("new_sl"),
            "new_tp": m.get("new_tp"),
            "new_volume": m.get("new_volume"),
        })

    return {"trades": out, "modifications": modifications}


class BridgeModificationAck(BaseModel):
    bridge_token: str
    trade_id: str
    type: str   # MODIFY_SL | PARTIAL_CLOSE
    success: bool = True
    new_sl: float | None = None
    new_volume: float | None = None
    error: str | None = None


@router.post("/modification-ack")
async def modification_ack(payload: BridgeModificationAck):
    """EA acknowledges it applied a pending_modification on its end."""
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    trade = await db.trades.find_one({"_id": ObjectId(payload.trade_id)})
    if not trade or trade["account_id"] != str(acc["_id"]):
        raise HTTPException(status_code=404, detail="Trade not found")

    update = {"pending_modification": None}
    if payload.success:
        if payload.type == "MODIFY_SL" and payload.new_sl is not None:
            update["stop_loss"] = float(payload.new_sl)
            if not trade.get("breakeven_set"):
                # Mark BE only when SL moved to/past entry
                entry = float(trade.get("entry_price") or 0)
                action = trade.get("action")
                hit_be = (action == "BUY" and payload.new_sl >= entry) or \
                         (action == "SELL" and payload.new_sl <= entry)
                if hit_be:
                    update["breakeven_set"] = True
            else:
                update["trail_active"] = True
        elif payload.type == "PARTIAL_CLOSE" and payload.new_volume is not None:
            update["lot_size"] = float(payload.new_volume)
            update["partial_closed"] = True
            # If this PC also carried a new_sl (Tier-1 combo move), apply it
            mod = trade.get("pending_modification") or {}
            mod_new_sl = mod.get("new_sl")
            if mod_new_sl is not None:
                update["stop_loss"] = float(mod_new_sl)
                update["breakeven_set"] = True
            # Mark tier progression
            if not trade.get("tp1_closed"):
                update["tp1_closed"] = True
            elif not trade.get("tp2_closed"):
                update["tp2_closed"] = True
        elif payload.type == "FULL_CLOSE":
            update["tp3_closed"] = True
    else:
        update["last_modification_error"] = payload.error or "unknown EA error"

    await db.trades.update_one({"_id": ObjectId(payload.trade_id)}, {"$set": update})
    await ws_manager.broadcast(acc["user_id"], "trade_updated", {
        "trade_id": payload.trade_id,
        **update,
    })
    return {"ok": True}


@router.post("/report")
async def report_trade(payload: BridgeTradeReport):
    db = get_db()
    acc = await _account_by_token(payload.bridge_token)
    trade = await db.trades.find_one({"_id": ObjectId(payload.trade_id)})
    if not trade or trade["account_id"] != str(acc["_id"]):
        raise HTTPException(status_code=404, detail="Trade not found")

    update = {"status": payload.status}
    if payload.mt5_ticket is not None:
        update["mt5_ticket"] = payload.mt5_ticket

    # Slippage veto — on first OPEN report, compare actual fill vs intended entry
    slippage_force_close = False
    if (
        payload.status == "open"
        and payload.entry_price is not None
        and not trade.get("slippage_checked")
    ):
        intended = float(trade.get("entry_price") or 0)
        actual = float(payload.entry_price)
        symbol = trade.get("symbol") or ""
        slip_pips = price_to_pips(symbol, abs(actual - intended)) if intended > 0 else 0.0
        update["intended_entry_price"] = intended
        update["slippage_pips"] = round(slip_pips, 2)
        update["slippage_checked"] = True
        # Pull bot config for the threshold
        cfg = await db.bot_configs.find_one({"user_id": acc["user_id"]}) or {}
        if cfg.get("slippage_veto_enabled", True):
            caps = cfg.get("max_slippage_pips") or {"XAUUSD": 20.0, "BTCUSD": 80.0}
            cap = float(caps.get(symbol, caps.get(symbol.upper(), 9999)))
            if slip_pips > cap:
                slippage_force_close = True
                update["pending_modification"] = {"type": "FULL_CLOSE"}
                update["close_reason"] = "slippage_veto"
                update["slippage_veto_cap_pips"] = cap

    if payload.entry_price is not None:
        update["entry_price"] = payload.entry_price
    if payload.exit_price is not None:
        update["exit_price"] = payload.exit_price
    if payload.pnl is not None:
        update["pnl"] = payload.pnl
    if payload.error:
        update["error"] = payload.error
    if payload.status == "closed":
        update["closed_at"] = datetime.now(timezone.utc).isoformat()
        # Infer close_reason if not already set (manual/panic/telegram set it pre-emptively).
        if not trade.get("close_reason"):
            entry = float(trade.get("entry_price") or 0)
            sl = float(trade.get("stop_loss") or 0)
            tp3 = float(trade.get("tp3") or trade.get("take_profit") or 0)
            exit_p = float(payload.exit_price or 0)
            action = trade.get("action")
            close_reason = "broker"
            if exit_p > 0 and entry > 0:
                # Within 0.1% of SL → SL hit. Within 0.1% of TP → TP hit.
                tol = max(entry * 0.001, 0.5)
                if sl > 0 and abs(exit_p - sl) <= tol:
                    close_reason = "stop_loss"
                elif tp3 > 0 and abs(exit_p - tp3) <= tol:
                    close_reason = "take_profit"
                else:
                    # Profit direction inference
                    profit_dir = (action == "BUY" and exit_p > entry) or (action == "SELL" and exit_p < entry)
                    close_reason = "take_profit" if profit_dir else "stop_loss"
            update["close_reason"] = close_reason

    await db.trades.update_one({"_id": ObjectId(payload.trade_id)}, {"$set": update})
    if slippage_force_close:
        try:
            await inc_intel_counter(acc["user_id"], "slippage_veto")
        except Exception:
            pass
    await ws_manager.broadcast(acc["user_id"], "trade_updated", {
        "trade_id": payload.trade_id,
        **update,
    })

    # Telegram alerts — fire-and-forget
    try:
        full_trade = await db.trades.find_one({"_id": ObjectId(payload.trade_id)})
        if full_trade:
            from notifier import notify_trade_opened, notify_trade_closed
            if payload.status == "open" and not trade.get("notified_opened"):
                await notify_trade_opened(acc["user_id"], full_trade)
                await db.trades.update_one(
                    {"_id": ObjectId(payload.trade_id)}, {"$set": {"notified_opened": True}}
                )
            elif payload.status == "closed":
                await notify_trade_closed(acc["user_id"], full_trade)
    except Exception:
        pass

    return {"ok": True}
