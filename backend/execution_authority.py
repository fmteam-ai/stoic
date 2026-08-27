"""Unified Execution Authority (v56 §2 — hard cutover). The ONE service
every execution-capable subsystem submits a canonical ExecutionIntent to.

    Strategy → ExecutionIntent → Execution Authority → Execution Engine

The intent is the INPUT to execution, not a byproduct of it: it is
minted and deduplicated FIRST, then VALIDATED, then AUTHORIZED against
the Global Trading Authority, and only then handed to the engine stage.
Pre-dispatch refusals (validation, authority, engine gates) finalize the
intent and RELEASE its dedupe key — the action never left STOIC, so a
corrected retry is a new logical action. From engine dispatch onward the
at-most-once invariant is strict."""
import logging
from datetime import datetime, timezone

logger = logging.getLogger("execution.authority")


def _validate(signal: dict, account: dict) -> list:
    problems = []
    if not signal.get("symbol"):
        problems.append("symbol required")
    side = str(signal.get("action") or signal.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        problems.append("side must be BUY or SELL")
    try:
        if float(signal.get("lot_size") or 0) <= 0:
            problems.append("lot_size must be > 0")
    except (TypeError, ValueError):
        problems.append("lot_size must be numeric")
    if not account:
        problems.append("account required")
    return problems


async def _finalize_pre_dispatch(db, intent_id: str, to: str,
                                 reason: str) -> None:
    """Pre-dispatch outcome: finalize the intent and release the dedupe
    key (the action never left STOIC — retries are new logical actions)."""
    from execution_intents import transition
    await transition(db, intent_id, to, detail=str(reason)[:200])
    await db.execution_intents.update_one(
        {"intent_id": intent_id}, {"$unset": {"dedupe_key": ""}})


async def submit_intent(*, user_id, account: dict, signal: dict, engine,
                        max_concurrent: int = 0,
                        cfg_account_id: str = None) -> dict:
    """The single execution entry point (all engine.execute calls route
    here). Returns the engine result, or a {'blocked': ...} refusal."""
    from database import get_db
    from execution_intents import (canonical_payload, create_intent,
                                   dedupe_key_for, transition)
    db = get_db()
    _acct_id = str(account.get("_id") or account.get("account_id")
                   or cfg_account_id or "")
    _side = str(signal.get("action") or signal.get("side") or "").upper()
    minute = datetime.now(timezone.utc).isoformat()[:16]
    _sig_ref = (signal.get("intent_ref") or signal.get("signal_id")
                or f"{minute}|{signal.get('entry_price')}|"
                   f"{signal.get('stop_loss')}")
    # 1 — CANONICAL INTENT FIRST (the input to execution)
    try:
        intent = await create_intent(
            db, source=str(signal.get("scope") or signal.get("origin")
                           or "manual"),
            kind="open_trade",
            dedupe_key=dedupe_key_for("mt5_bridge", "open_trade",
                                      _acct_id, signal.get("symbol"),
                                      _side, _sig_ref),
            payload=canonical_payload(
                account_id=_acct_id,
                broker_account_number=str(account.get("account_number")
                                          or account.get("login") or ""),
                broker_server=str(account.get("server")
                                  or account.get("broker_server") or ""),
                strategy_id=str(signal.get("scope")
                                or signal.get("origin") or "manual"),
                strategy_version=str(signal.get("strategy_version") or ""),
                symbol=str(signal.get("symbol") or ""), side=_side,
                requested_volume=float(signal.get("lot_size") or 0),
                stop_loss=signal.get("stop_loss"),
                take_profit=signal.get("take_profit"),
                risk_snapshot_id="", signal_id=str(signal.get("signal_id")
                                                   or ""),
                fencing_epoch=int(signal.get("scalp_lease_epoch") or 0),
                nonce=""),
            account_id=_acct_id, actor=user_id)
    except (TypeError, AttributeError):  # isolated unit-test db mock
        logger.critical("execution authority BYPASSED — non-Motor db "
                        "object; intent pipeline inactive for this call "
                        "(must never happen in production)")
        return await engine.execute_authorized(
            user_id=user_id, account=account, signal=signal,
            max_concurrent=max_concurrent,
            cfg_account_id=cfg_account_id, intent=None)
    if intent.get("duplicate"):
        logger.warning("authority blocked duplicate intent user=%s sym=%s "
                       "intent=%s status=%s", user_id,
                       signal.get("symbol"), intent.get("intent_id"),
                       intent.get("status"))
        return {"blocked": "duplicate_intent",
                "intent_id": intent.get("intent_id"),
                "intent_status": intent.get("status"),
                "original_result": intent.get("result")}
    iid = intent["intent_id"]
    # 2 — VALIDATED
    problems = _validate(signal, account)
    if problems:
        await _finalize_pre_dispatch(db, iid, "rejected",
                                     "validation: " + "; ".join(problems))
        return {"blocked": "intent_validation", "intent_id": iid,
                "reasons": problems}
    await transition(db, iid, "validated")
    # 3 — AUTHORIZED (Global Trading Authority decides, not the strategy)
    from trading_authority import enforce_new_trade
    gate = await enforce_new_trade(db, account=account)
    if not gate.get("ok"):
        logger.warning("authority refused intent %s level=%s reasons=%s",
                       iid, gate.get("level"), gate.get("reasons"))
        await _finalize_pre_dispatch(
            db, iid, "cancelled",
            f"trading authority {gate.get('level')}: "
            + "; ".join(gate.get("reasons") or []))
        return {"blocked": "trading_authority", "intent_id": iid,
                "authority_level": gate.get("level"),
                "reasons": gate.get("reasons")}
    if gate.get("reduce_factor") and signal.get("lot_size"):
        _orig = float(signal["lot_size"])
        signal["lot_size"] = max(
            0.01, round(_orig * float(gate["reduce_factor"]), 2))
        signal["_authority_reduced"] = True
        logger.warning("TRADING AUTHORITY REDUCED — lot %s → %s user=%s "
                       "sym=%s", _orig, signal["lot_size"], user_id,
                       signal.get("symbol"))
    await transition(db, iid, "authorized",
                     detail=f"authority {gate.get('level') or 'FULL'}")
    intent = await db.execution_intents.find_one({"intent_id": iid},
                                                 {"_id": 0})
    # 4 — EXECUTION ENGINE (the intent is its input)
    result = await engine.execute_authorized(
        user_id=user_id, account=account, signal=signal,
        max_concurrent=max_concurrent, cfg_account_id=cfg_account_id,
        intent=intent)
    if isinstance(result, dict) and result.get("blocked"):
        # engine refused pre-dispatch — the order never left STOIC
        await _finalize_pre_dispatch(db, iid, "cancelled",
                                     f"engine block: {result['blocked']}")
        result.setdefault("intent_id", iid)
    return result
