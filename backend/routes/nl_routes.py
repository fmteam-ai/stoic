"""Natural-Language Commander routes.

Two POST endpoints powered by Claude:
  /api/nl/strategy   — translate user prompt → bot_config (preview, save optional)
  /api/nl/command    — translate user prompt → actions, execute against the bot

Plus a polling sweeper that reads `db.conditional_triggers` on each bot_runner
tick and fires `then` actions when the condition is met.
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user
from database import get_db
from ws_manager import manager as ws_manager
from nl_commander import build_strategy, interpret_command
from strategy_backtest import run_backtest
from strategy_code_generator import generate_code
from strategy_optimizer import optimize as optimize_strategy
from route_utils import parse_object_id

import logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nl", tags=["nl-commander"])


# ------------------- Strategy Builder -------------------------------------
@router.post("/strategy")
async def nl_strategy(payload: dict, user=Depends(get_current_user)):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt required")
    if len(prompt) > 2000:
        raise HTTPException(status_code=400, detail="prompt too long")

    try:
        result = await build_strategy(prompt)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI compile failed: {e}")

    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])

    return {"compiled": result, "prompt": prompt}


@router.post("/strategy/code")
async def nl_strategy_code(payload: dict, user=Depends(get_current_user)):  # noqa: ARG001
    """Expand a compiled NL strategy into a DSL + pseudocode block.

    Body: {"compiled": {...}}
    Returns the DSL (closed-vocab JSON) + a Python-flavoured pseudocode block.
    Refuses on empty/clarification-only compiles.
    """
    compiled = payload.get("compiled") or {}
    if not compiled or compiled.get("clarification_needed"):
        raise HTTPException(status_code=400, detail="No usable compiled strategy")
    try:
        dsl = await generate_code(compiled)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Code generation failed: {e}")
    if dsl.get("error"):
        raise HTTPException(status_code=502, detail=dsl["error"])
    return {"dsl": dsl}


@router.post("/strategy/optimize")
async def nl_strategy_optimize(payload: dict, user=Depends(get_current_user)):
    """Grid-search filter variants of a DSL and return the best by score.

    Body: {"dsl": {...}}    # DSL produced by /strategy/code (or compiled with symbols)
    Returns baseline + best variant + tested variants + transparency notes.
    """
    dsl = payload.get("dsl") or payload.get("compiled") or {}
    if not dsl:
        raise HTTPException(status_code=400, detail="dsl (or compiled) required")
    if not dsl.get("symbols"):
        raise HTTPException(status_code=400, detail="dsl.symbols required")
    try:
        result = await optimize_strategy(dsl=dsl, user_id=user["id"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Optimization failed: {e}")
    return result


@router.post("/strategy/backtest")
async def nl_strategy_backtest(payload: dict, user=Depends(get_current_user)):
    """Replay a compiled strategy against the user's own closed-trade history.

    Body: {"compiled": {...}, "lookback_days": 30}
    Returns: win_rate, total/avg P&L, per-symbol breakdown, sample size, notes.
    Refuses to run on an empty/clarification-only compile.
    """
    compiled = payload.get("compiled") or {}
    if not compiled or compiled.get("clarification_needed"):
        raise HTTPException(status_code=400, detail="No usable compiled strategy")
    lookback_days = int(payload.get("lookback_days") or 30)
    result = await run_backtest(
        compiled=compiled, user_id=user["id"], lookback_days=lookback_days,
    )
    return result

@router.get("/strategy/targets")
async def nl_strategy_targets(symbols: str = "", user=Depends(get_current_user)):
    """List candidate bot_configs annotated with whether each one's symbols
    overlap the comma-separated `symbols` param. Powers the target-selector
    dropdown on the Strategies page (no proposal_id required).
    """
    db = get_db()
    from research_agent.proposal_targeting import list_candidate_bots
    syms = [s.strip().upper() for s in (symbols or "").split(",") if s.strip()]
    candidates = await list_candidate_bots(db, user["id"], syms)
    matching = sum(1 for c in candidates if c["matches_proposal_symbols"])
    return {
        "proposal_symbols": syms,
        "candidates": candidates,
        "matching_count": matching,
        "total_count": len(candidates),
    }



@router.post("/strategy/apply")
async def nl_strategy_apply(payload: dict, user=Depends(get_current_user)):
    """Persist a compiled strategy into the user's bot_config(s).

    Body: {compiled: dict, target: str (default 'matching')}
      target ∈ "matching" | "all" | "default" | "<account_id>"
    """
    compiled = payload.get("compiled") or {}
    target = (payload.get("target") or "matching").strip()
    if not compiled or compiled.get("clarification_needed"):
        raise HTTPException(status_code=400, detail="No usable compiled strategy")

    db = get_db()
    from research_agent.proposal_targeting import (
        resolve_target_configs, apply_to_bot_configs,
    )
    update_fields = {
        "risk_level": compiled.get("risk_level", "medium"),
        "symbols": [s.upper() for s in (compiled.get("symbols") or ["XAUUSD", "BTCUSD"])],
        "max_concurrent_trades": int(compiled.get("max_concurrent_trades", 2)),
        "auto_execute": bool(compiled.get("auto_execute", True)),
        "session_preference": compiled.get("session_preference", "any"),
        "strategy_style": compiled.get("strategy_style", "trend_following"),
        "source": "nl_strategy_builder",
    }
    configs, resolved_mode = await resolve_target_configs(
        db, user["id"], update_fields["symbols"], target,
    )
    if not configs:
        raise HTTPException(
            status_code=422,
            detail=f"No matching bot configs for target='{target}'. "
                   "Try target='all' or pick a specific bot.",
        )
    audit = await apply_to_bot_configs(
        db, configs,
        update_fields=update_fields,
        source="nl_strategy",
        source_id=None,
        target_mode=resolved_mode,
        auto=False,
    )
    # Return the first updated config for legacy clients that read `config`.
    cfg = await db.bot_configs.find_one({"_id": configs[0]["_id"]})
    if cfg:
        cfg["id"] = str(cfg.pop("_id"))
    return {
        "ok": True,
        "config": cfg,
        "applied_count": len(audit),
        "target_mode": resolved_mode,
        "applied_to": audit,
    }


# ------------------- Risk Commander (NL Circuit Breakers) ------------------
# iter-151 — live-sensitive NL actions require explicit operator approval
SENSITIVE_NL_ACTIONS = {"SET_RISK_LEVEL", "ENABLE_BOTS", "CLOSE_ALL_TRADES"}
KNOWN_NL_ACTIONS = {"DISABLE_BOTS", "ENABLE_BOTS", "MOVE_STOPS_BREAKEVEN",
                    "CLOSE_ALL_TRADES", "SET_RISK_LEVEL", "PANIC_LOCK",
                    "SET_CONDITIONAL_TRIGGER"}


@router.post("/command/confirm")
async def nl_command_confirm(payload: dict, user=Depends(get_current_user)):
    """Iter-151 · execute a previously proposed action set after the
    operator explicitly approved it in the Risk Commander UI."""
    actions = payload.get("actions") or []
    if not actions:
        raise HTTPException(status_code=400, detail="actions required")
    for a in actions:
        if str(a.get("type") or "").upper() not in KNOWN_NL_ACTIONS:
            raise HTTPException(status_code=400,
                                detail=f"unknown action type {a.get('type')}")
    receipts = await _execute_actions(user["id"], actions)
    return {"summary": f"Approved — executed {len(receipts)} action(s).",
            "receipts": receipts, "actions": actions, "confirmed": True}


@router.post("/command")
async def nl_command(payload: dict, user=Depends(get_current_user)):
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt required")
    if len(prompt) > 2000:
        raise HTTPException(status_code=400, detail="prompt too long")

    try:
        parsed = await interpret_command(prompt)
    except Exception as e:
        logger.warning("nl interpret failed: %s", e)
        raise HTTPException(status_code=502, detail={
            "code": "ai_interpret_failed",
            "message": "The AI could not interpret that command — try rephrasing."})

    if parsed.get("clarification_needed"):
        return {"clarification_needed": parsed["clarification_needed"], "prompt": prompt}
    if parsed.get("error"):
        raise HTTPException(status_code=502, detail=parsed["error"])

    actions = parsed.get("actions") or []
    if not actions:
        raise HTTPException(status_code=400, detail="No actions produced from prompt")

    # iter-151 — AI → PROPOSAL → operator approval for live-sensitive
    # actions (risk raises, re-enabling bots, mass closes). Safety-reducing
    # actions (disable, panic, breakeven) still execute immediately.
    sensitive = [a for a in actions
                 if str(a.get("type") or "").upper() in SENSITIVE_NL_ACTIONS]
    if sensitive and not payload.get("confirm"):
        return {"requires_confirmation": True,
                "summary": ("These action(s) change live trading behaviour "
                            "and need your explicit approval."),
                "pending_actions": actions,
                "sensitive_types": sorted({str(a.get("type")).upper()
                                           for a in sensitive}),
                "prompt": prompt}

    receipts = await _execute_actions(user["id"], actions)
    summary = parsed.get("summary") or "Commands executed."
    await ws_manager.broadcast(user["id"], "nl_command_executed", {
        "summary": summary, "receipts": receipts, "prompt": prompt,
    })
    return {"summary": summary, "receipts": receipts, "actions": actions, "prompt": prompt}


@router.get("/triggers")
async def list_triggers(user=Depends(get_current_user)):
    db = get_db()
    cursor = db.conditional_triggers.find({"user_id": user["id"], "active": True})
    docs = await cursor.to_list(length=100)
    out = []
    for d in docs:
        d["id"] = str(d.pop("_id"))
        out.append(d)
    return out


@router.delete("/triggers/{trigger_id}")
async def delete_trigger(trigger_id: str, user=Depends(get_current_user)):
    db = get_db()
    await db.conditional_triggers.update_one(
        {"_id": parse_object_id(trigger_id, "Trigger"), "user_id": user["id"]},
        {"$set": {"active": False}},
    )
    return {"ok": True}


# ------------------- Action Executors --------------------------------------
async def _execute_actions(user_id: str, actions: list) -> list:
    receipts = []
    for act in actions:
        a_type = (act.get("type") or "").upper()
        target = act.get("target") or "all"
        params = act.get("params") or {}
        try:
            if a_type == "DISABLE_BOTS":
                r = await _disable_bots(user_id, target)
            elif a_type == "ENABLE_BOTS":
                r = await _enable_bots(user_id, target)
            elif a_type == "MOVE_STOPS_BREAKEVEN":
                r = await _move_stops_breakeven(user_id, target)
            elif a_type == "CLOSE_ALL_TRADES":
                r = await _close_all_trades(user_id, target)
            elif a_type == "SET_RISK_LEVEL":
                r = await _set_risk_level(user_id, params.get("risk_level", "low"))
            elif a_type == "PANIC_LOCK":
                from routes.panic_routes import _disable_all_bots_and_close_trades
                r = await _disable_all_bots_and_close_trades(
                    {"user_id": user_id}, broadcast_user_id=user_id
                )
            elif a_type == "SET_CONDITIONAL_TRIGGER":
                r = await _save_trigger(user_id, params)
            else:
                r = {"skipped": True, "reason": f"unknown action {a_type}"}
            receipts.append({"type": a_type, "target": target, "result": r})
        except Exception as e:
            logger.warning("nl action %s failed: %s", a_type, e)
            receipts.append({"type": a_type, "target": target,
                             "error": "action_failed"})
    return receipts


async def _disable_bots(user_id, target):
    db = get_db()
    q = {"user_id": user_id}
    if target == "high_risk":
        q["risk_level"] = {"$in": ["high", "extreme"]}
    update = {"active": False, "tripped_at": datetime.now(timezone.utc).isoformat(),
              "tripped_reason": f"NL command: disable {target}"}
    res = await db.bot_configs.update_many(q, {"$set": update})
    return {"bots_disabled": res.modified_count}


async def _enable_bots(user_id, target):
    db = get_db()
    q = {"user_id": user_id}
    if target == "high_risk":
        q["risk_level"] = {"$in": ["high", "extreme"]}
    res = await db.bot_configs.update_many(q, {"$set": {
        "active": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }})
    return {"bots_enabled": res.modified_count}


async def _move_stops_breakeven(user_id, target):
    db = get_db()
    q = {"user_id": user_id, "status": "open"}
    if target and target not in ("all", ""):
        q["symbol"] = target.upper()
    cursor = db.trades.find(q)
    trades = await cursor.to_list(length=500)
    updated = 0
    for t in trades:
        new_sl = t["entry_price"]
        await db.trades.update_one(
            {"_id": t["_id"]},
            {"$set": {"stop_loss": new_sl, "sl_adjustment": "nl_breakeven",
                      "sl_updated_at": datetime.now(timezone.utc).isoformat()}},
        )
        updated += 1
    return {"trades_updated": updated, "new_stop": "entry_price"}


async def _close_all_trades(user_id, target):
    db = get_db()
    q = {"user_id": user_id, "status": {"$in": ["open", "pending"]}}
    if target and target not in ("all", ""):
        q["symbol"] = target.upper()
    res = await db.trades.update_many(q, {"$set": {
        "close_requested": True,
        "close_reason": "nl_command",
    }})
    return {"trades_marked_for_close": res.modified_count}


async def _set_risk_level(user_id, risk_level):
    """Risk Commander: set risk level on ALL bots the user owns.

    Default behaviour is broadcast (matches the NL intent "set risk to low"
    — the user means it for all their bots). If a per-bot target is ever
    needed, the AI orchestrator can call `_apply_to_target` directly.
    """
    if risk_level not in ("low", "medium", "high", "extreme"):
        return {"error": f"invalid risk_level {risk_level}"}
    db = get_db()
    from research_agent.proposal_targeting import (
        resolve_target_configs, apply_to_bot_configs,
    )
    configs, resolved_mode = await resolve_target_configs(
        db, user_id, [], "all",
    )
    if not configs:
        return {"risk_level": risk_level, "modified": 0,
                "note": "no bot configs found"}
    audit = await apply_to_bot_configs(
        db, configs,
        update_fields={"risk_level": risk_level},
        source="risk_commander",
        source_id=None,
        target_mode=resolved_mode,
        auto=False,
    )
    return {"risk_level": risk_level, "modified": len(audit),
            "target_mode": resolved_mode}


async def _save_trigger(user_id, params):
    db = get_db()
    doc = {
        "user_id": user_id,
        "symbol": (params.get("symbol") or "BTCUSD").upper(),
        "condition": params.get("condition", "drop"),
        "threshold_pct": float(params.get("threshold_pct", 3.0)),
        "then": params.get("then", []),
        "active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "baseline_price": None,  # filled on first sweep
    }
    r = await db.conditional_triggers.insert_one(doc)
    return {"trigger_id": str(r.inserted_id), "symbol": doc["symbol"],
            "condition": doc["condition"], "threshold_pct": doc["threshold_pct"]}
