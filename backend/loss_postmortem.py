"""Loss post-mortem — investigates losing trades and feeds learnings back.

Triggered when a trade closes with either:
  • close_reason="stop_loss" AND pnl < 0  (real edge failure)
  • pnl < 0  AND  prior closed trade on same (user, symbol) also negative
    (consecutive-loss streak — pattern likely to repeat)

Pipeline:
  1. Quantitative diff   — regime, macro (DXY/VIX/yields/sentiment), session,
                           A+ / MTF gates: entry-time vs now.
  2. Claude narrative    — 4-section structured post-mortem with a `pattern_key`
                           for clustering.
  3. Pattern aggregation — count post-mortems per pattern_key in last 30d.
  4. Guardrail tightening — when count ≥ 3 AND user has `auto_tighten_enabled`,
                            bump `min_confidence_override` on the relevant
                            bot_config by +5 (clamped 50-95), with a 7-day
                            cooldown per pattern so we don't ratchet on noise.

Stored in:
  • `loss_postmortems`         — one doc per losing-trade investigation
  • `guardrail_adjustments`    — audit trail of auto-tightens
  • `users.postmortem_settings = {auto_tighten_enabled: bool}` — opt-in
"""
import os
import json
import logging
import uuid
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from database import get_db
from emergentintegrations.llm.chat import LlmChat, UserMessage

logger = logging.getLogger("loss-postmortem")

_POSTMORTEM_SYSTEM = """You are a forensic trading-strategy analyst. Given a
losing trade's entry conditions and the current market state, return STRICT
JSON with this shape:

{
  "summary": "1-sentence executive summary",
  "why_it_looked_good": "What signals justified entering this trade (rehash)",
  "what_actually_happened": "Price-action story between entry and stop-out",
  "what_changed": "What flipped in macro/regime/sentiment/session between entry and now",
  "lessons": ["Short actionable lesson 1", "Lesson 2", "Lesson 3"],
  "suggested_guardrail": "ONE concrete config tweak (e.g. raise min_confidence by 5 in NY session for XAUUSD BUY)",
  "pattern_key": "<symbol>|<entry_regime>|<entry_session>|<action>"
}

Be terse. Do NOT use markdown. Do NOT prefix the JSON. Only output the JSON object."""


def _pattern_key(trade: dict, signal: dict) -> str:
    """Stable cluster key per loss-pattern. Lets us count recurrences."""
    regime = (signal.get("regime") or {})
    if isinstance(regime, dict):
        regime_label = regime.get("regime") or "UNKNOWN"
    else:
        regime_label = str(regime) or "UNKNOWN"
    session = (signal.get("session") or {})
    if isinstance(session, dict):
        session_label = (session.get("primary") or "off").upper()
    else:
        session_label = str(session).upper() or "OFF"
    return f"{trade.get('symbol','?')}|{regime_label}|{session_label}|{trade.get('action','?')}"


async def _is_postmortem_eligible(db, trade: dict) -> tuple[bool, str]:
    """Return (eligible, trigger_reason).

    iter-127c (user request): EVERY closed losing trade gets a post-mortem.
    The trigger label still distinguishes how it lost:
      • close_reason contains 'stop_loss'      → 'sl_hit'
      • previous closed trade on symbol lost   → 'consecutive_loss'
      • any other loss                         → 'loss'
    """
    pnl = float(trade.get("pnl") or 0)
    if pnl >= 0:
        return False, ""
    if "stop_loss" in (trade.get("close_reason") or "").lower():
        return True, "sl_hit"
    prior = await db.trades.find_one(
        {
            "user_id": trade["user_id"],
            "symbol": trade["symbol"],
            "status": "closed",
            "_id": {"$ne": trade["_id"]},
        },
        sort=[("closed_at", -1)],
    )
    if prior and float(prior.get("pnl") or 0) < 0:
        return True, "consecutive_loss"
    return True, "loss"


def _quantitative_diff(signal: dict, current_macro: dict, current_session: dict) -> dict:
    """Side-by-side entry vs now snapshot. No LLM — pure numeric diff."""
    entry_macro = signal.get("macro") or {}
    entry_session = signal.get("session") or {}
    entry_sentiment = (signal.get("sentiment") or {}).get("score")
    cur_sentiment = (current_macro.get("sentiment") or {}).get("score")
    cur_summary = current_macro.get("summary") or {}
    return {
        "regime_at_entry": (signal.get("regime") or {}).get("regime") if isinstance(signal.get("regime"), dict) else signal.get("regime"),
        "session_at_entry": (entry_session.get("primary") if isinstance(entry_session, dict) else None) or "off",
        "session_now": (current_session.get("primary") if isinstance(current_session, dict) else None) or "off",
        "macro_freeze_at_entry": bool(entry_macro.get("frozen")),
        "macro_gate_open_now": cur_summary.get("gate_open"),
        "sentiment_at_entry": entry_sentiment,
        "sentiment_now": cur_sentiment,
        "sentiment_flipped": (
            entry_sentiment is not None and cur_sentiment is not None
            and ((entry_sentiment > 0) != (cur_sentiment > 0))
        ),
        "dxy_at_entry": (entry_macro.get("dxy") or {}).get("value") if isinstance(entry_macro.get("dxy"), dict) else None,
        "dxy_now": cur_summary.get("dxy"),
        "vix_now": cur_summary.get("vix"),
        "y10y_now": cur_summary.get("y10y"),
        "mtf_aligned_at_entry": (signal.get("mtf_gate") or {}).get("aligned"),
        "aplus_passed_at_entry": (signal.get("aplus_confluence") or {}).get("passed"),
        "confidence_at_entry": signal.get("confidence"),
    }


async def _claude_narrative(trade: dict, signal: dict, diff: dict) -> dict:
    """Call Claude for the structured 4-section narrative. Cheap, ~1k tokens."""
    user_text = json.dumps({
        "trade": {
            "symbol": trade.get("symbol"),
            "action": trade.get("action"),
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "stop_loss": trade.get("stop_loss"),
            "take_profit": trade.get("take_profit"),
            "lot_size": trade.get("lot_size"),
            "pnl": trade.get("pnl"),
            "close_reason": trade.get("close_reason"),
            "opened_at": trade.get("opened_at") or trade.get("created_at"),
            "closed_at": trade.get("closed_at"),
        },
        "signal_reasoning_at_entry": (signal.get("reasoning") or "")[:400],
        "quantitative_diff": diff,
    }, default=str)

    try:
        chat = LlmChat(
            api_key=os.environ["EMERGENT_LLM_KEY"],
            session_id=f"postmortem-{trade.get('symbol','?')}-{uuid.uuid4().hex[:8]}",
            system_message=_POSTMORTEM_SYSTEM,
        ).with_model("anthropic", "claude-sonnet-4-5-20250929")
        response = await chat.send_message(UserMessage(text=user_text))
        # Strip code fences / prefix garbage just in case
        raw = str(response).strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("postmortem LLM failed: %s", e)
        return {
            "summary": f"Loss on {trade.get('symbol')} {trade.get('action')} ({trade.get('close_reason')})",
            "why_it_looked_good": (signal.get("reasoning") or "")[:200] or "n/a",
            "what_actually_happened": f"Stopped at {trade.get('exit_price')} for ${trade.get('pnl')}",
            "what_changed": "LLM analysis unavailable — review the quantitative diff manually.",
            "lessons": [],
            "suggested_guardrail": "",
            "pattern_key": _pattern_key(trade, signal),
            "_llm_failed": True,
        }


async def _user_doc(db, user_id: str) -> dict | None:
    """Find user row tolerating ObjectId vs string `_id` history."""
    try:
        d = await db.users.find_one({"_id": ObjectId(user_id)})
        if d:
            return d
    except Exception:
        pass
    return (await db.users.find_one({"_id": user_id})
            or await db.users.find_one({"id": user_id}))


async def maybe_record_postmortem(db, trade_id, force: bool = False) -> dict | None:
    """Run a post-mortem if the trade is eligible. Idempotent — skips if a
    postmortem for this trade already exists.

    `force=True` (manual user request): any closed losing trade qualifies —
    the sl_hit/consecutive-loss heuristic only gates AUTO post-mortems.

    Returns the inserted postmortem doc (or existing one) on success, None on
    skip.
    """
    if isinstance(trade_id, str):
        try:
            trade_id = ObjectId(trade_id)
        except Exception:
            return None
    trade = await db.trades.find_one({"_id": trade_id})
    if not trade or trade.get("status") != "closed":
        return None

    # Idempotent
    existing = await db.loss_postmortems.find_one({"trade_id": str(trade_id)})
    if existing:
        return existing

    eligible, trigger = await _is_postmortem_eligible(db, trade)
    if not eligible:
        if not (force and float(trade.get("pnl") or 0) < 0):
            return None
        trigger = "manual"

    # Look up the original signal (best-effort)
    signal = {}
    sig_id = trade.get("signal_id")
    if sig_id:
        try:
            signal = await db.signals.find_one({"_id": ObjectId(sig_id)}) or {}
        except Exception:
            pass

    # Current market snapshot — best effort, fall back to empty.
    cur_macro = {}
    cur_session = {}
    try:
        from macro_feeds import get_macro_snapshot
        cur_macro = await get_macro_snapshot() or {}
    except Exception:
        pass
    try:
        from ai_signals import current_session
        cur_session = current_session()
    except Exception:
        pass

    diff = _quantitative_diff(signal, cur_macro, cur_session)
    narrative = await _claude_narrative(trade, signal, diff)
    # Always derive pattern_key ourselves — Claude's free-text version is
    # unreliable for clustering (varies in case + ordering). Overwrite any
    # value the LLM emitted with our canonical key.
    pattern_key = _pattern_key(trade, signal)
    narrative["pattern_key"] = pattern_key

    doc = {
        "user_id": trade["user_id"],
        "trade_id": str(trade_id),
        "symbol": trade.get("symbol"),
        "action": trade.get("action"),
        "pnl": float(trade.get("pnl") or 0),
        "close_reason": trade.get("close_reason"),
        "account_id": trade.get("account_id"),
        "pattern_key": pattern_key,
        "trigger": trigger,
        "diff": diff,
        "narrative": narrative,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = await db.loss_postmortems.insert_one(doc)
    doc["_id"] = result.inserted_id

    # Auto-tighten guardrails if pattern is recurring and the user opted in.
    try:
        adjustment = await _maybe_autotighten(db, trade, signal, pattern_key)
        if adjustment:
            doc["adjustment"] = adjustment
    except Exception as e:  # noqa: BLE001
        logger.warning("auto-tighten failed for trade %s: %s", trade_id, e)

    return doc


async def _maybe_autotighten(db, trade: dict, signal: dict, pattern_key: str) -> dict | None:
    """When pattern_key has ≥3 post-mortems in the last 30 days AND the user
    opted in via `users.postmortem_settings.auto_tighten_enabled`, bump
    min_confidence_override on the matching bot_config by +5 (clamped 50-95).

    Per-pattern cooldown of 7 days prevents ratcheting on noise.
    """
    user_id = trade["user_id"]
    user = await _user_doc(db, user_id)
    settings = (user or {}).get("postmortem_settings") or {}
    if not settings.get("auto_tighten_enabled"):
        return None

    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    count = await db.loss_postmortems.count_documents({
        "user_id": user_id,
        "pattern_key": pattern_key,
        "created_at": {"$gte": cutoff},
    })
    if count < 3:
        return None

    # Cooldown — has this pattern been auto-tightened in the last 7 days?
    cooldown_cut = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    recent_adj = await db.guardrail_adjustments.find_one({
        "user_id": user_id,
        "pattern_key": pattern_key,
        "created_at": {"$gte": cooldown_cut},
    })
    if recent_adj:
        return None

    # Pick the right cfg: per-account first if trade has account_id, else default.
    cfg_filter = {"user_id": user_id}
    acct_id = trade.get("account_id")
    if acct_id:
        cfg_filter["account_id"] = acct_id
    cfg = await db.bot_configs.find_one(cfg_filter)
    if not cfg and acct_id:
        cfg = await db.bot_configs.find_one({"user_id": user_id, "account_id": None})
    if not cfg:
        return None

    cur_min = int(cfg.get("min_confidence_override") or 0)
    new_min = min(95, max(50, (cur_min if cur_min > 0 else 65) + 5))
    if new_min <= cur_min:
        return None

    await db.bot_configs.update_one(
        {"_id": cfg["_id"]},
        {"$set": {"min_confidence_override": new_min,
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    adj_doc = {
        "user_id": user_id,
        "config_id": str(cfg["_id"]),
        "pattern_key": pattern_key,
        "direction": "tighten",
        "field": "min_confidence_override",
        "from": cur_min,
        "to": new_min,
        "trigger_count": count,
        "trigger_kind": "losing_pattern",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.guardrail_adjustments.insert_one(adj_doc)
    logger.warning(
        "Auto-tightened user=%s cfg=%s pattern=%s min_confidence %d→%d",
        user_id, cfg["_id"], pattern_key, cur_min, new_min,
    )

    # Telegram alert — fire-and-forget
    try:
        from notifier import send_telegram
        await send_telegram(
            user_id,
            "auto_guardrail",
            "Auto-Guardrail Tightened",
            [
                f"Pattern: {pattern_key}",
                f"{count} losses in 30d — bot self-tightened.",
                f"min_confidence_override: {cur_min} → {new_min}",
                "View at /loss-lab",
            ],
        )
    except Exception:
        pass

    return adj_doc


async def maybe_record_winner(db, trade_id) -> dict | None:
    """Auto-loosen counterpart to `maybe_record_postmortem`.

    Called on EVERY trade close. If the trade was a winner AND its pattern_key
    had previously been auto-tightened, we count winners after the most recent
    tighten — when that count hits 3, ease `min_confidence_override` back by
    -3 (clamped at 50) so the bot doesn't get trapped at an over-cautious
    threshold once the market conditions that caused the tighten have passed.

    Mirrors `_maybe_autotighten`'s guards:
      • only fires when the user has `auto_tighten_enabled=true` (same opt-in)
      • 7-day per-pattern cooldown against re-loosening
      • clamped at 50 (never below the user-configurable floor)
    """
    if isinstance(trade_id, str):
        try:
            trade_id = ObjectId(trade_id)
        except Exception:
            return None
    trade = await db.trades.find_one({"_id": trade_id})
    if not trade or trade.get("status") != "closed":
        return None
    if float(trade.get("pnl") or 0) <= 0:
        return None  # losses are handled by `maybe_record_postmortem`

    user_id = trade["user_id"]
    user = await _user_doc(db, user_id)
    settings = (user or {}).get("postmortem_settings") or {}
    if not settings.get("auto_tighten_enabled"):
        return None  # opt-in gate — same flag governs both directions

    # Reconstruct the trade's pattern_key from its origin signal (best-effort).
    signal = {}
    sig_id = trade.get("signal_id")
    if sig_id:
        try:
            signal = await db.signals.find_one({"_id": ObjectId(sig_id)}) or {}
        except Exception:
            pass
    pattern_key = _pattern_key(trade, signal)

    # Find the most recent TIGHTEN for this pattern (the one we'd un-do).
    last_tighten = await db.guardrail_adjustments.find_one(
        {
            "user_id": user_id,
            "pattern_key": pattern_key,
            "$or": [{"direction": "tighten"}, {"direction": {"$exists": False}}],
        },
        sort=[("created_at", -1)],
    )
    if not last_tighten:
        return None
    # If the most recent adjustment for this pattern is already a loosen, skip.
    last_any = await db.guardrail_adjustments.find_one(
        {"user_id": user_id, "pattern_key": pattern_key},
        sort=[("created_at", -1)],
    )
    if last_any and last_any.get("direction") == "loosen":
        return None

    # Count winning closed trades on this pattern AFTER the tighten.
    win_cutoff = last_tighten["created_at"]
    wins_after = await db.trades.count_documents({
        "user_id": user_id,
        "status": "closed",
        "symbol": trade["symbol"],
        "pnl": {"$gt": 0},
        "closed_at": {"$gte": win_cutoff},
    })
    if wins_after < 3:
        return None

    # 7-day cooldown — don't ratchet loosen ↔ tighten on edge-case streaks.
    cooldown_cut = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    recent_loosen = await db.guardrail_adjustments.find_one({
        "user_id": user_id,
        "pattern_key": pattern_key,
        "direction": "loosen",
        "created_at": {"$gte": cooldown_cut},
    })
    if recent_loosen:
        return None

    # Pick the matching cfg
    cfg_filter = {"user_id": user_id}
    acct_id = trade.get("account_id")
    if acct_id:
        cfg_filter["account_id"] = acct_id
    cfg = await db.bot_configs.find_one(cfg_filter)
    if not cfg and acct_id:
        cfg = await db.bot_configs.find_one({"user_id": user_id, "account_id": None})
    if not cfg:
        return None

    cur_min = int(cfg.get("min_confidence_override") or 0)
    if cur_min == 0:
        return None  # nothing to loosen — never had a tighten applied
    new_min = max(50, cur_min - 3)
    if new_min >= cur_min:
        return None  # already at floor

    await db.bot_configs.update_one(
        {"_id": cfg["_id"]},
        {"$set": {"min_confidence_override": new_min,
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    adj_doc = {
        "user_id": user_id,
        "config_id": str(cfg["_id"]),
        "pattern_key": pattern_key,
        "direction": "loosen",
        "field": "min_confidence_override",
        "from": cur_min,
        "to": new_min,
        "trigger_count": wins_after,
        "trigger_kind": "winning_streak",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.guardrail_adjustments.insert_one(adj_doc)
    logger.info(
        "Auto-loosened user=%s cfg=%s pattern=%s min_confidence %d→%d (after %d wins)",
        user_id, cfg["_id"], pattern_key, cur_min, new_min, wins_after,
    )

    try:
        from notifier import send_telegram
        await send_telegram(
            user_id,
            "auto_guardrail",
            "Auto-Guardrail Loosened",
            [
                f"Pattern: {pattern_key}",
                f"{wins_after} wins after the last tighten — bot self-loosened.",
                f"min_confidence_override: {cur_min} → {new_min}",
                "View at /loss-lab",
            ],
        )
    except Exception:
        pass

    return adj_doc
