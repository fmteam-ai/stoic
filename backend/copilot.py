"""AI Co-Pilot — grounded in the user's live trading state.

Every request snapshots the user's: bot config, last N signals, last N trades,
account balances, current regime/sentiment, panic state, active triggers — and
hands it to Claude Sonnet 4.5 as JSON context. Multi-turn history persisted
in `db.copilot_sessions` keyed by (user_id, session_id).
"""
import os
import uuid
import json
from datetime import datetime, timezone

from database import get_db
from llm_models import provider_model


SYSTEM_PROMPT = """You are the user's personal Trading Co-Pilot for an AI trading
bot platform (XAUUSD + BTCUSD; can be extended). You answer questions about
their LIVE account state, explain signals, flag risks, and help them adjust.

You receive a JSON snapshot of the user's current state before each question.

Rules:
- Be concise. 2-5 sentences usually. Use bullet points for lists.
- Reference SPECIFIC numbers from the snapshot ($, %, lots, regime names).
- If asked "why HOLD", cite the actual veto chain (entropy / macro / meta-labeler / news).
- If the user asks to CHANGE settings, tell them: "I can't change settings — use
  the Risk Commander page (/commander) to issue a natural-language command, or
  Bot Config for manual changes."
- If the snapshot lacks the data needed, say so honestly — don't invent.
- For new users with no trades, encourage them to start in Paper mode.
- Never recommend specific BUY/SELL trades — that's the bot's job, not yours.
- Markdown is supported for formatting but keep it lightweight.
"""


async def _build_context_snapshot(user_id: str) -> dict:
    """One-shot read of everything that's relevant for grounding."""
    db = get_db()
    snap = {"now": datetime.now(timezone.utc).isoformat()}

    # Bot config
    cfg = await db.bot_configs.find_one({"user_id": user_id})
    if cfg:
        snap["bot_config"] = {
            "active": cfg.get("active", False),
            "risk_level": cfg.get("risk_level"),
            "symbols": cfg.get("symbols", []),
            "auto_execute": cfg.get("auto_execute"),
            "max_concurrent_trades": cfg.get("max_concurrent_trades"),
            "tripped_reason": cfg.get("tripped_reason"),
        }

    # Accounts (balances + connection state)
    accounts = await db.accounts.find({"user_id": user_id}).to_list(length=20)
    snap["accounts"] = [
        {
            "label": a.get("label"),
            "broker": a.get("broker"),
            "mode": a.get("mode", "live"),
            "balance": a.get("balance"),
            "currency": a.get("currency"),
            "ea_connected": bool(a.get("last_heartbeat_at")),
            "last_heartbeat_at": a.get("last_heartbeat_at"),
        }
        for a in accounts
    ]

    # Last 5 signals
    sigs = await db.signals.find({"user_id": user_id}).sort("created_at", -1).limit(5).to_list(length=5)
    snap["recent_signals"] = [
        {
            "symbol": s.get("symbol"),
            "action": s.get("action"),
            "confidence": s.get("confidence"),
            "regime": (s.get("regime") or {}).get("regime"),
            "exec_mode": (s.get("regime_execution_mode") or {}).get("execution_mode"),
            "noise": (s.get("noise_filter") or {}).get("label"),
            "meta_verdict": (s.get("meta_label") or {}).get("verdict"),
            "meta_p_true": (s.get("meta_label") or {}).get("p_true"),
            "veto_applied": s.get("veto_applied"),
            "reasoning": (s.get("reasoning") or "")[:280],
            "created_at": s.get("created_at"),
        }
        for s in sigs
    ]

    # Open + recent trades
    open_trades = await db.trades.find({"user_id": user_id, "status": "open"}).to_list(length=20)
    recent_trades = await db.trades.find({"user_id": user_id}).sort("opened_at", -1).limit(5).to_list(length=5)
    snap["open_trades"] = [
        {"symbol": t.get("symbol"), "side": t.get("side"), "qty": t.get("quantity"),
         "entry": t.get("entry_price"), "sl": t.get("stop_loss"), "tp": t.get("take_profit"),
         "mode": t.get("mode"), "unrealised_pnl": t.get("unrealised_pnl")}
        for t in open_trades
    ]
    snap["recent_trades"] = [
        {"symbol": t.get("symbol"), "side": t.get("side"), "pnl": t.get("pnl"),
         "status": t.get("status"), "opened_at": t.get("opened_at"),
         "closed_at": t.get("closed_at"), "mode": t.get("mode")}
        for t in recent_trades
    ]

    # Panic state
    panic = await db.panic_state.find_one({"user_id": user_id}) or {}
    snap["panic"] = {"active": bool(panic.get("active")), "reason": panic.get("reason")}

    # Active conditional triggers
    triggers = await db.conditional_triggers.find(
        {"user_id": user_id, "active": True}
    ).to_list(length=20)
    snap["active_triggers"] = [
        {"symbol": t.get("symbol"), "condition": t.get("condition"),
         "threshold_pct": t.get("threshold_pct")}
        for t in triggers
    ]

    return snap


async def get_or_create_session(user_id: str, session_id: str | None) -> dict:
    db = get_db()
    if session_id:
        doc = await db.copilot_sessions.find_one(
            {"user_id": user_id, "session_id": session_id}
        )
        if doc:
            return doc
    new_sid = uuid.uuid4().hex
    doc = {
        "user_id": user_id,
        "session_id": new_sid,
        "messages": [],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.copilot_sessions.insert_one(doc)
    return doc


async def chat(user_id: str, message: str, session_id: str | None = None) -> dict:
    """Run a single Co-Pilot turn. Returns {session_id, answer, snapshot_used}."""
    db = get_db()
    session = await get_or_create_session(user_id, session_id)
    snapshot = await _build_context_snapshot(user_id)

    # Compose grounded system message
    grounded_system = (
        SYSTEM_PROMPT
        + "\n\nCURRENT USER STATE (JSON snapshot, just now):\n"
        + json.dumps(snapshot, default=str, indent=2)
    )

    from emergentintegrations.llm.chat import LlmChat, UserMessage
    chat_client = LlmChat(
        api_key=os.environ["EMERGENT_LLM_KEY"],
        session_id=session["session_id"],
        system_message=grounded_system,
    ).with_model(*provider_model("copilot"))

    # Replay last 6 turns of history (3 user + 3 assistant) for continuity.
    # NOTE: emergentintegrations LlmChat persists by session_id across processes,
    # so we just send the new user message. Older history is kept in our DB.
    response = await chat_client.send_message(UserMessage(text=message))
    answer = str(response).strip()

    # Append to our DB history (we keep our own copy for the UI)
    await db.copilot_sessions.update_one(
        {"user_id": user_id, "session_id": session["session_id"]},
        {
            "$push": {
                "messages": {
                    "$each": [
                        {"role": "user", "content": message,
                         "ts": datetime.now(timezone.utc).isoformat()},
                        {"role": "assistant", "content": answer,
                         "ts": datetime.now(timezone.utc).isoformat()},
                    ]
                }
            },
            "$set": {"last_used_at": datetime.now(timezone.utc).isoformat()},
        },
    )

    return {
        "session_id": session["session_id"],
        "answer": answer,
        "snapshot": snapshot,
    }


async def list_sessions(user_id: str) -> list:
    db = get_db()
    cursor = db.copilot_sessions.find({"user_id": user_id}).sort("last_used_at", -1).limit(20)
    docs = await cursor.to_list(length=20)
    out = []
    for d in docs:
        msgs = d.get("messages") or []
        out.append({
            "session_id": d["session_id"],
            "created_at": d.get("created_at"),
            "last_used_at": d.get("last_used_at"),
            "message_count": len(msgs),
            "preview": (msgs[0]["content"][:80] if msgs else ""),
        })
    return out


async def get_session(user_id: str, session_id: str) -> dict | None:
    db = get_db()
    doc = await db.copilot_sessions.find_one(
        {"user_id": user_id, "session_id": session_id}
    )
    if not doc:
        return None
    return {
        "session_id": doc["session_id"],
        "messages": doc.get("messages", []),
        "created_at": doc.get("created_at"),
    }
