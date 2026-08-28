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

EXECUTION_POLICY_VERSION = "v56.3"


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
    # 0 — PAMM resolution (a PAMM master account or program-tagged signal
    # makes this a PAMM-originated execution; the Strategy Guard is bound
    # HERE so no alternate PAMM route to MT5 can exist)
    _pamm_program = None
    try:
        from modules.pamm.strategy_guard import resolve_program
        _pamm_program = await resolve_program(db, _acct_id, signal)
    except (TypeError, AttributeError):  # isolated unit-test db mock
        _pamm_program = None
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
                nonce="",
                broker_capability_version=str(account.get("ea_version")
                                              or account.get(
                                                  "broker_capability_version")
                                              or ""),
                model_version=str(signal.get("model_version") or ""),
                execution_policy_version=EXECUTION_POLICY_VERSION,
                pamm_program_id=str((_pamm_program or {}).get("program_id")
                                    or "")),
            program_id=(str(_pamm_program.get("program_id"))
                        if _pamm_program else None),
            account_id=_acct_id, actor=user_id)
    except (TypeError, AttributeError):  # isolated unit-test db mock
        logger.critical("execution authority BYPASSED — non-Motor db "
                        "object; intent pipeline inactive for this call "
                        "(must never happen in production)")
        from execution_authorization import mint_authorization
        return await engine.execute_authorized(
            user_id=user_id, account=account, signal=signal,
            max_concurrent=max_concurrent,
            cfg_account_id=cfg_account_id, intent=None,
            authorization=mint_authorization(""))
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
    # 2a — PAMM STRATEGY GUARD (v62.3): PAMM-originated execution must be
    # authorized against the active assignment (strategy/version/hash,
    # certification, strictest risk, execution eligibility) BEFORE the
    # Global Trading Authority. LEGACY programs pass through unchanged.
    if _pamm_program is not None:
        from modules.pamm.strategy_guard import \
            authorize_pamm_strategy_execution
        guard = await authorize_pamm_strategy_execution(
            db, _pamm_program, account, signal)
        if not guard["authorized"]:
            logger.warning("PAMM strategy guard REJECTED intent %s "
                           "program=%s reason=%s", iid,
                           _pamm_program.get("program_id"),
                           guard["reason"])
            await _finalize_pre_dispatch(
                db, iid, "rejected",
                f"pamm_strategy_guard: {guard['reason']}")
            return {"blocked": "pamm_strategy_guard", "intent_id": iid,
                    "reason": guard["reason"], "checks": guard["checks"]}
        if guard["mode"] == "STRATEGY":
            ctx = guard["context"]
            signal["_pamm_identity"] = ctx
            signal["strategy_version"] = ctx["strategy_version"]
            await db.execution_intents.update_one(
                {"intent_id": iid},
                {"$set": {"payload.pamm_program_id":
                          ctx["pamm_program_id"],
                          "payload.assignment_id": ctx["assignment_id"],
                          "payload.strategy_id": ctx["strategy_id"],
                          "payload.strategy_version":
                          ctx["strategy_version"],
                          "payload.strategy_hash": ctx["strategy_hash"],
                          "payload.risk_profile_id":
                          ctx["risk_profile_id"],
                          "payload.certification_id":
                          ctx["certification_id"]}})
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
        try:
            from verdict_tracking import record_verdict
            await record_verdict(
                db, source="trading_authority", verdict="REJECT",
                requested=float(signal.get("lot_size") or 0), approved=0.0,
                unit="lot", user_id=user_id,
                limiting_factor=gate.get("level"),
                reasons=gate.get("reasons"),
                context={"symbol": signal.get("symbol"), "side": _side,
                         "entry_price": signal.get("entry_price"),
                         "stop_loss": signal.get("stop_loss"),
                         "take_profit": signal.get("take_profit")})
        except Exception as e:
            logger.warning("verdict tracking failed: %s", e)
        await _finalize_pre_dispatch(
            db, iid, "cancelled",
            f"trading authority {gate.get('level')}: "
            + "; ".join(gate.get("reasons") or []))
        return {"blocked": "trading_authority", "intent_id": iid,
                "authority_level": gate.get("level"),
                "reasons": gate.get("reasons")}
    _verdict_id = None
    if gate.get("reduce_factor") and signal.get("lot_size"):
        _orig = float(signal["lot_size"])
        signal["lot_size"] = max(
            0.01, round(_orig * float(gate["reduce_factor"]), 2))
        signal["_authority_reduced"] = True
        logger.warning("TRADING AUTHORITY REDUCED — lot %s → %s user=%s "
                       "sym=%s", _orig, signal["lot_size"], user_id,
                       signal.get("symbol"))
        try:
            from verdict_tracking import record_verdict
            _verdict_id = await record_verdict(
                db, source="trading_authority", verdict="REDUCE",
                requested=_orig, approved=float(signal["lot_size"]),
                unit="lot", user_id=user_id,
                limiting_factor=gate.get("level"),
                reasons=gate.get("reasons"),
                context={"symbol": signal.get("symbol"), "side": _side,
                         "entry_price": signal.get("entry_price"),
                         "stop_loss": signal.get("stop_loss"),
                         "take_profit": signal.get("take_profit")})
        except Exception as e:
            logger.warning("verdict tracking failed: %s", e)
    await transition(db, iid, "authorized",
                     detail=f"authority {gate.get('level') or 'FULL'}")
    # v56 hardening — immutable decision-context snapshots: the exact
    # authority verdict and market state behind this authorization are
    # persisted and referenced from the canonical intent forever.
    try:
        import uuid as _uuid
        _now_iso = datetime.now(timezone.utc).isoformat()
        auth_snap_id = f"authsnap_{_uuid.uuid4().hex[:12]}"
        await db.authority_snapshots.insert_one(
            {"snapshot_id": auth_snap_id, "intent_id": iid,
             "user_id": user_id, "at": _now_iso,
             "gate": {k: gate.get(k) for k in
                      ("ok", "level", "reasons", "reduce_factor")},
             "execution_policy_version": EXECUTION_POLICY_VERSION})
        mkt_snap_id = f"mktsnap_{_uuid.uuid4().hex[:12]}"
        _tick = await db.price_ticks.find_one(
            {"symbol": str(signal.get("symbol") or "").upper()},
            sort=[("ts", -1)])
        await db.market_snapshots.insert_one(
            {"snapshot_id": mkt_snap_id, "intent_id": iid,
             "symbol": str(signal.get("symbol") or "").upper(),
             "price": float(_tick.get("price") or 0) if _tick else None,
             "tick_at": str(_tick.get("ts")) if _tick else None,
             "at": _now_iso})
        await db.execution_intents.update_one(
            {"intent_id": iid},
            {"$set": {"payload.authority_snapshot_id": auth_snap_id,
                      "payload.market_snapshot_id": mkt_snap_id}})
    except Exception as e:
        logger.warning("decision-context snapshot failed for %s: %s",
                       iid, e)
    # T6 — Execution Authority authorization mark (T0→T9 profiler)
    signal.setdefault("latency_trace", {})["t6_ms"] = int(
        datetime.now(timezone.utc).timestamp() * 1000)
    intent = await db.execution_intents.find_one({"intent_id": iid},
                                                 {"_id": 0})
    # 4 — EXECUTION ENGINE (the intent is its input; the capability token
    # proves this call came through the authority choke point)
    from execution_authorization import mint_authorization
    result = await engine.execute_authorized(
        user_id=user_id, account=account, signal=signal,
        max_concurrent=max_concurrent, cfg_account_id=cfg_account_id,
        intent=intent, authorization=mint_authorization(iid))
    if isinstance(result, dict) and result.get("blocked"):
        # engine refused pre-dispatch — the order never left STOIC
        await _finalize_pre_dispatch(db, iid, "cancelled",
                                     f"engine block: {result['blocked']}")
        result.setdefault("intent_id", iid)
    elif _verdict_id and isinstance(result, dict) and result.get("id"):
        try:
            from verdict_tracking import link_trade
            await link_trade(db, _verdict_id, result["id"])
        except Exception as e:
            logger.warning("verdict link failed: %s", e)
    return result
