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

EXECUTION_POLICY_VERSION = "v56.4"


def account_execution_lock(account: dict) -> str | None:
    """Account-role execution rule (audit correction) — pure, unit-testable.

        STANDARD      → normal STOIC execution rules
        PAMM_MASTER   → must flow through the PAMM strategy guard
                        (certification → risk truth → authority → intent)
        PAMM_INVESTOR → MONITOR ONLY: Execution Authority LOCKED,
                        STOIC never places an order on investor accounts.

    Returns the lock reason, or None when execution may proceed."""
    role = str((account or {}).get("account_role") or "STANDARD").upper()
    if role == "PAMM_INVESTOR":
        return ("account_role=PAMM_INVESTOR is MONITOR ONLY — Execution "
                "Authority is LOCKED; STOIC never places orders on "
                "investor accounts")
    return None


def account_enablement_lock(account: dict | None) -> str | None:
    """AT-01 — the canonical choke point refuses any account whose
    `trading_enabled` is not EXPLICITLY True (False, missing or any other
    type = OFF). Worker selection filters the same predicate; this makes the
    intent layer agree even if a caller bypasses selection."""
    if (account or {}).get("trading_enabled") is not True:
        return ("trading_enabled is not explicitly true on this account — "
                "execution refused (missing/false = OFF)")
    return None


def _validate(signal: dict, account: dict) -> list:
    problems = []
    if not signal.get("symbol"):
        problems.append("symbol required")
    side = str(signal.get("action") or signal.get("side") or "").upper()
    if side not in ("BUY", "SELL"):
        problems.append("side must be BUY or SELL")
    # SEC v62.7 — this plane only ever OPENS exposure. An order that
    # self-labels as risk-reducing is fraudulent here (those labels are
    # a guard bypass for safety exits, which never flow through this
    # plane). Reject structurally, never by convention.
    if (signal.get("reduce_only") or signal.get("close_trade")
            or signal.get("pamm_risk_reducing")
            or signal.get("intent") in ("close", "reduce")):
        problems.append("risk-reducing labels are not accepted on the "
                        "open-trade execution plane")
    try:
        if float(signal.get("lot_size") or 0) <= 0:
            problems.append("lot_size must be > 0")
    except (TypeError, ValueError):
        problems.append("lot_size must be numeric")
    if not account:
        problems.append("account required")
    return problems


def _is_crypto_amount(account: dict | None, signal: dict) -> bool:
    """True when `lot_size` is an exchange base-asset amount (crypto/CCXT
    engines), not an MT5 lot."""
    return ((account or {}).get("kind") or "").lower() == "binance" \
        or str(signal.get("broker_kind") or "").lower() == "binance" \
        or "_crypto_requested_amount" in signal


async def _finalize_pre_dispatch(db, intent_id: str, to: str,
                                 reason: str) -> None:
    """Pre-dispatch outcome: finalize the intent and release the dedupe
    key (the action never left STOIC — retries are new logical actions)."""
    from execution_intents import transition
    await transition(db, intent_id, to, detail=str(reason)[:200])
    await db.execution_intents.update_one(
        {"intent_id": intent_id}, {"$unset": {"dedupe_key": ""}})


async def _fail_closed(db, code: str, exc: Exception, user_id, acct_id,
                       signal: dict) -> dict:
    """Audit P0-2 — the execution plane never fails open. Refuse, page
    operations (critical ops alert), return a typed BLOCKED outcome with
    a correlation id. Zero engine calls, zero authorization minted."""
    import uuid
    corr = f"execfail_{uuid.uuid4().hex[:12]}"
    logger.critical("EXECUTION REFUSED (%s) %s — %s: %s user=%s acct=%s "
                    "sym=%s", code, corr, type(exc).__name__, exc, user_id,
                    acct_id, signal.get("symbol"))
    try:
        from alerting import raise_alert
        await raise_alert(
            db, "execution_pipeline_failure", "critical",
            f"execution plane refused a trade ({code}): "
            f"{type(exc).__name__}: {str(exc)[:160]} [{corr}]",
            dedup_key=f"execution_pipeline_failure:{code}",
            meta={"correlation_id": corr, "user_id": str(user_id),
                  "account_id": acct_id, "symbol": signal.get("symbol")})
    except Exception as ae:  # noqa: BLE001 — alert path must not mask refusal
        logger.error("could not raise execution failure alert: %s", ae)
    return {"blocked": code, "correlation_id": corr,
            "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}


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
    # 00 — ACCOUNT ROLE LOCK (audit correction): enforced in the backend
    # execution plane, never merely hidden in the UI. Refused before any
    # intent is minted — a monitor-only account must leave zero execution
    # artifacts.
    _enable_lock = account_enablement_lock(account)
    if _enable_lock:
        logger.warning("execution refused: account %s user %s symbol %s — %s",
                       _acct_id, user_id, signal.get("symbol"), _enable_lock)
        return {"blocked": "account_not_enabled", "reason": _enable_lock}
    _role_lock = account_execution_lock(account)
    if _role_lock:
        logger.warning("execution REFUSED — %s (account=%s user=%s sym=%s)",
                       _role_lock, _acct_id, user_id, signal.get("symbol"))
        return {"blocked": "account_role_locked", "reason": _role_lock}
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
        # account-declared program binding (PAMM role model)
        if _pamm_program is None and account.get("pamm_program_id"):
            _pamm_program = await db.pamm_programs.find_one(
                {"program_id": str(account["pamm_program_id"])})
    except Exception as e:  # noqa: BLE001 — FAIL CLOSED (audit P0-2)
        return await _fail_closed(db, "pamm_resolution_error", e,
                                  user_id, _acct_id, signal)
    # A PAMM_MASTER account may ONLY execute through a certified PAMM
    # program (strategy guard chain). No resolved program = no execution.
    if (str(account.get("account_role") or "STANDARD").upper()
            == "PAMM_MASTER" and _pamm_program is None):
        logger.warning("execution REFUSED — PAMM_MASTER account %s has no "
                       "resolvable PAMM program (user=%s sym=%s)",
                       _acct_id, user_id, signal.get("symbol"))
        return {"blocked": "pamm_master_unbound",
                "reason": "account_role=PAMM_MASTER may only execute "
                          "through a certified PAMM program — no program "
                          "resolved for this account/signal"}
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
    except Exception as e:  # noqa: BLE001 — FAIL CLOSED (audit P0-2)
        # Any intent-pipeline failure (db wrapper, programming error,
        # dependency) refuses execution: no intent, no authorization,
        # no engine call. Test doubles must implement the real protocol.
        return await _fail_closed(db, "intent_pipeline_error", e,
                                  user_id, _acct_id, signal)
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
                          ctx["certification_id"],
                          "payload.risk_snapshot_id":
                          ctx.get("risk_snapshot_id") or ""}})
        elif guard["mode"] == "MANUAL_OVERRIDE":
            signal["_pamm_identity"] = guard["context"]
            await db.execution_intents.update_one(
                {"intent_id": iid},
                {"$set": {"payload.pamm_program_id":
                          guard["context"]["pamm_program_id"],
                          "payload.pamm_manual_override": True,
                          "payload.risk_snapshot_id":
                          guard["context"].get("risk_snapshot_id") or ""}})
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
    try:
        gate = await enforce_new_trade(db, account=account)
    except Exception as e:  # noqa: BLE001 — FAIL CLOSED (audit P0-2)
        await _finalize_pre_dispatch(db, iid, "cancelled",
                                     f"authority evaluation error: {e}")
        return await _fail_closed(db, "authority_evaluation_error", e,
                                  user_id, _acct_id, signal)
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
        _rf = float(gate["reduce_factor"])
        if _is_crypto_amount(account, signal):
            # crypto: lot_size is a BASE-ASSET amount (0.001 BTC) — scale it
            # down only; exchange precision is applied by the engine. Never
            # a 0.01 floor (that was a 10× INCREASE for 0.001 BTC).
            import math as _m
            _new = _m.floor(_orig * _rf * 1e8) / 1e8
        else:
            # MT5 lots: floor to the volume step; below the minimum → 0.0
            from risk import scale_lot
            _new = scale_lot(_orig, _rf)
        if _new <= 0 or _new > _orig:
            await _finalize_pre_dispatch(
                db, iid, "cancelled",
                f"trading authority {gate.get('level')} reduce left "
                f"{_new} < minimum")
            return {"blocked": "trading_authority_reduce_below_min_lot",
                    "intent_id": iid, "authority_level": gate.get("level"),
                    "reasons": gate.get("reasons")}
        signal["lot_size"] = _new
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
        auth_snap_id = gate.get("snapshot_id") or \
            f"authsnap_{_uuid.uuid4().hex[:12]}"
        await db.authority_snapshots.insert_one(
            {"snapshot_id": auth_snap_id, "intent_id": iid,
             "user_id": user_id, "at": _now_iso,
             "gate": {k: gate.get(k) for k in
                      ("ok", "level", "reasons", "reduce_factor",
                       "domains")},
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


# ══════════════════════════════════════════════════════════════════════
# Atomic exposure reservations (concurrency + open-risk caps)
# ══════════════════════════════════════════════════════════════════════
# Replaces the racy `count_documents(...) → insert_one(...)` cap check:
# two concurrent submitters could both count N-1 and both insert. Every
# NEW position now atomically reserves a slot (and its stop-risk $) on a
# per-scope counter document BEFORE its trade row is inserted:
#
#   exposure_reservations doc  (_id = "acct:<account_id>" | "user:<user_id>")
#     {auto_slots, total_slots, risk_usd, rev, holds: {<hold_id>: {...}},
#      rebuilt_at}
#
#   reserve  : find_one_and_update({_id, auto_slots<$cap, total_slots<$tot,
#              risk_usd<=max_risk-new_risk}, {$inc ..., $set holds.<id>})
#   release  : conditional on holds.<id> existing → idempotent
#   rebuild  : recompute from open/pending trades (+ young unbound holds),
#              CAS on `rev` so it never clobbers a concurrent reservation.
#
# Engine-agnostic API (MT5 bridge, paper, crypto/CCXT, any future engine):
#
#   res = await reserve_exposure(db, user_id=..., account_id=...,
#             cfg_account_id=..., symbol=..., lot=..., risk_usd=...,
#             auto=True, max_concurrent=N, max_total=N+2,
#             max_risk_usd=equity*9%)
#   if not res["ok"]: return res            # {"blocked": "...", ...}
#   trade_doc["exposure_reservation"] = res["reservation"]   # may be None
#   try:    insert the trade row
#   except: await release_reservation(db, res["reservation"]); raise
#   ...on close / cancel / reject of that trade:
#   await release_reservation(db, trade_doc)
#
# Missing releases cannot leak capacity permanently: bot_runner calls
# `rebuild_reservations` at the start of every tick (and reserve seeds a
# missing doc via rebuild).
import inspect as _inspect
import os as _os
import uuid as _uuid_mod

RESERVATIONS = "exposure_reservations"
OPEN_STATES = ["pending", "open"]
# an unbound hold (reserved, trade row not yet visible) survives a rebuild
# this long — the reserve→insert window is milliseconds.
HOLD_TTL_SEC = float(_os.environ.get("EXPOSURE_HOLD_TTL_SEC", "180"))


def scope_key(user_id, cfg_account_id=None) -> str:
    """Key of the cap scope — mirrors the legacy count query: per account
    when the bot config is account-scoped, user-wide otherwise."""
    return (f"acct:{cfg_account_id}" if cfg_account_id
            else f"user:{user_id}")


def account_key(account_id) -> str:
    return f"acct:{account_id}"


def _hold_id(trade: dict) -> str:
    res = trade.get("exposure_reservation") or {}
    if isinstance(res, dict) and res.get("id"):
        return str(res["id"])
    return f"trade:{trade.get('_id') or trade.get('id')}"


def _scope_query(key: str) -> dict:
    kind, _, ident = key.partition(":")
    q = {"status": {"$in": OPEN_STATES}}
    q["account_id" if kind == "acct" else "user_id"] = ident
    return q


def trade_stop_risk_usd(trade: dict, account: dict | None = None,
                        equity: float = 0.0) -> float:
    """Stop-distance risk of a position — SAME math as the Safety
    Guardian's aggregate open-risk check (unknown/no stop = charged the
    per-trade maximum, never $0)."""
    res = trade.get("exposure_reservation") or {}
    if isinstance(res, dict) and res.get("risk_usd") is not None:
        try:
            return max(0.0, float(res["risk_usd"]))
        except (TypeError, ValueError):
            pass
    from pip_utils import price_to_pips, pip_value_usd_per_lot
    from safety_guardian import MAX_RISK_PCT_PER_TRADE
    try:
        lot = float(trade.get("lot_size") or 0)
        entry = float(trade.get("entry_price") or 0)
        sl = float(trade.get("stop_loss") or 0)
        sym = trade.get("symbol") or ""
        if lot > 0 and entry > 0 and sl > 0:
            return (price_to_pips(sym, abs(entry - sl))
                    * pip_value_usd_per_lot(
                        sym, (account or {}).get("account_type"),
                        price=entry) * lot)
        if lot > 0:
            return equity * (MAX_RISK_PCT_PER_TRADE / 100.0)
        return 0.0
    except Exception:  # noqa: BLE001 — unparseable = charged conservatively
        return equity * (MAX_RISK_PCT_PER_TRADE / 100.0)


def _maybe_awaitable(x):
    return x if _inspect.isawaitable(x) else None


class ReservationStoreUnavailable(RuntimeError):
    """The db handle is a non-async test double (no motor protocol)."""


async def _call(coll, method: str, *a, **k):
    res = getattr(coll, method)(*a, **k)
    aw = _maybe_awaitable(res)
    if aw is None:
        raise ReservationStoreUnavailable(method)
    return await aw


async def rebuild_reservations(db, account_id=None, *, user_id=None,
                               account: dict | None = None,
                               key: str | None = None) -> dict:
    """Self-healing recompute of one scope's counters from the trades
    collection (open + pending), keeping unbound holds younger than
    HOLD_TTL_SEC. CAS on `rev`: if a reservation/release raced the
    rebuild, the rebuild is skipped (the next tick retries).

    Pass ``account_id`` (account scope) or ``user_id`` (user-wide scope)
    or an explicit ``key``. Returns the stored counters."""
    key = key or (account_key(account_id) if account_id
                  else f"user:{user_id}")
    coll = getattr(db, RESERVATIONS)
    now = datetime.now(timezone.utc)
    cur = await _call(coll, "find_one", {"_id": key})
    equity = float((account or {}).get("equity")
                   or (account or {}).get("balance") or 0)
    holds: dict = {}
    auto = total = 0
    risk = 0.0
    cursor = db.trades.find(_scope_query(key),
                            {"_id": 1, "origin": 1, "lot_size": 1,
                             "entry_price": 1, "stop_loss": 1, "symbol": 1,
                             "exposure_reservation": 1})
    trades = await _call(cursor, "to_list", length=None)
    for t in trades:
        hid = _hold_id(t)
        r = trade_stop_risk_usd(t, account, equity)
        is_auto = t.get("origin") == "auto"
        holds[hid] = {"auto": is_auto, "risk_usd": round(r, 6),
                      "trade_id": str(t.get("_id")), "at": now.isoformat()}
        total += 1
        auto += 1 if is_auto else 0
        risk += r
    unbound = [hid for hid, h in ((cur or {}).get("holds") or {}).items()
               if hid not in holds and not h.get("trade_id")
               and not hid.startswith("trade:")]
    settled: set = set()
    if unbound:
        # a reservation whose trade row exists but is no longer open
        # (closed/cancelled/rejected without a release) is a leak — drop it
        rows = await _call(db.trades.find(
            {"exposure_reservation.id": {"$in": unbound}},
            {"exposure_reservation": 1}), "to_list", length=None)
        settled = {_hold_id(r) for r in rows}
    for hid, h in ((cur or {}).get("holds") or {}).items():
        if hid in holds or hid not in unbound or hid in settled:
            continue
        try:
            age = (now - datetime.fromisoformat(str(h.get("at")))
                   ).total_seconds()
        except (TypeError, ValueError):
            age = HOLD_TTL_SEC + 1
        if age <= HOLD_TTL_SEC:            # in-flight reserve→insert
            holds[hid] = h
            total += 1
            auto += 1 if h.get("auto") else 0
            risk += float(h.get("risk_usd") or 0)
    body = {"auto_slots": auto, "total_slots": total,
            "risk_usd": round(risk, 6), "holds": holds,
            "rebuilt_at": now.isoformat()}
    if cur is None:
        try:
            await _call(coll, "insert_one", {"_id": key, "rev": 0, **body})
        except Exception as e:  # noqa: BLE001 — DuplicateKey: raced seed
            if "duplicate" not in str(e).lower() and \
                    type(e).__name__ != "DuplicateKeyError":
                raise
            return {"key": key, "rebuilt": False, "reason": "raced_seed"}
        return {"key": key, "rebuilt": True, **body}
    res = await _call(coll, "update_one",
                      {"_id": key, "rev": cur.get("rev", 0)},
                      {"$set": body, "$inc": {"rev": 1}})
    ok = bool(getattr(res, "modified_count", 1))
    if ok and (cur.get("total_slots") != total
               or cur.get("auto_slots") != auto):
        logger.warning("exposure reservations healed key=%s slots %s/%s → "
                       "%s/%s", key, cur.get("auto_slots"),
                       cur.get("total_slots"), auto, total)
    return {"key": key, "rebuilt": ok, **body,
            **({} if ok else {"reason": "concurrent_update"})}


async def _reserve_one(db, key: str, hold_id: str, hold: dict, *,
                       max_concurrent: int, max_total: int | None,
                       max_risk_usd: float | None, account: dict | None):
    coll = getattr(db, RESERVATIONS)
    risk = float(hold.get("risk_usd") or 0)
    flt: dict = {"_id": key}
    if max_concurrent and max_concurrent > 0:
        flt["auto_slots"] = {"$lt": int(max_concurrent)}
    if max_total and max_total > 0:
        flt["total_slots"] = {"$lt": int(max_total)}
    if max_risk_usd is not None:
        flt["risk_usd"] = {"$lte": float(max_risk_usd) - risk}
    upd = {"$inc": {"auto_slots": 1 if hold.get("auto") else 0,
                    "total_slots": 1, "risk_usd": risk, "rev": 1},
           "$set": {f"holds.{hold_id}": hold}}
    for attempt in (0, 1):
        doc = await _call(coll, "find_one_and_update", flt, upd)
        if doc is not None:
            return True, doc
        cur = await _call(coll, "find_one", {"_id": key})
        if cur is not None:
            return False, cur
        if attempt == 0:   # seed the scope from the trades collection
            await rebuild_reservations(db, key=key, account=account)
    return False, None


async def reserve_exposure(db, *, user_id, account_id, symbol: str,
                           lot: float, risk_usd: float, auto: bool,
                           cfg_account_id=None, max_concurrent: int = 0,
                           max_total: int | None = None,
                           max_risk_usd: float | None = None,
                           account: dict | None = None) -> dict:
    """Atomically reserve one position slot + its stop risk.

    Caps: ``auto_slots < max_concurrent`` and ``total_slots < max_total``
    on the CAP SCOPE doc (scope_key(user_id, cfg_account_id)); open-risk
    ``risk_usd + new ≤ max_risk_usd`` on the ACCOUNT doc. When the two
    keys coincide (account-scoped bots) it is a single atomic update; else
    the second reservation failing compensates the first.

    Returns {"ok": True, "reservation": {...} | None} or a typed block
    {"ok": False, "blocked": "max_concurrent_cap" | "open_risk_cap" |
    "reservation_error", ...}. ``reservation`` is None only when the db
    handle is a non-async test double (the legacy count gate still ran).
    Real database errors FAIL CLOSED."""
    rid = f"rsv_{_uuid_mod.uuid4().hex[:16]}"
    hold = {"auto": bool(auto), "risk_usd": round(float(risk_usd or 0), 6),
            "symbol": str(symbol or ""), "lot": float(lot or 0),
            "trade_id": None,
            "at": datetime.now(timezone.utc).isoformat()}
    skey = scope_key(user_id, cfg_account_id)
    akey = account_key(account_id) if account_id else skey
    plan = ([(skey, max_concurrent, max_total, max_risk_usd)]
            if skey == akey else
            [(skey, max_concurrent, max_total, None),
             (akey, 0, None, max_risk_usd)])
    done: list = []
    try:
        for key, mc, mt, mr in plan:
            ok, doc = await _reserve_one(
                db, key, rid, hold, max_concurrent=mc, max_total=mt,
                max_risk_usd=mr,
                account=account if key == akey else None)
            if not ok:
                for k in done:
                    await _release_key(db, k, rid)
                d = doc or {}
                cap_hit = ((mc and int(d.get("auto_slots") or 0) >= mc)
                           or (mt and int(d.get("total_slots") or 0) >= mt))
                if cap_hit or mr is None:
                    return {"ok": False, "blocked": "max_concurrent_cap",
                            "inflight": int(d.get("auto_slots") or 0),
                            "cap": max_concurrent,
                            "total_inflight": int(d.get("total_slots") or 0),
                            "total_cap": max_total,
                            "reservation_key": key, "atomic": True}
                return {"ok": False, "blocked": "open_risk_cap",
                        "open_risk_usd": round(float(d.get("risk_usd")
                                                     or 0), 2),
                        "new_risk_usd": hold["risk_usd"],
                        "max_risk_usd": round(float(mr), 2),
                        "reservation_key": key, "atomic": True}
            done.append(key)
    except ReservationStoreUnavailable:
        logger.error("exposure reservation store unavailable (non-async db "
                     "handle) — relying on the legacy count gate only")
        return {"ok": True, "reservation": None,
                "skipped": "reservation_store_unavailable"}
    except Exception as e:  # noqa: BLE001 — FAIL CLOSED
        for k in done:
            try:
                await _release_key(db, k, rid)
            except Exception:  # noqa: BLE001
                pass
        logger.critical("exposure reservation FAILED — refusing user=%s "
                        "acct=%s sym=%s: %s", user_id, account_id, symbol, e)
        return {"ok": False, "blocked": "reservation_error",
                "reason": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"ok": True, "reservation": {"id": rid, "keys": done,
                                        "risk_usd": hold["risk_usd"],
                                        "auto": hold["auto"]}}


async def _release_key(db, key: str, hold_id: str) -> bool:
    coll = getattr(db, RESERVATIONS)
    cur = await _call(coll, "find_one", {"_id": key,
                                         f"holds.{hold_id}": {"$exists": True}})
    if not cur:
        return False
    h = (cur.get("holds") or {}).get(hold_id) or {}
    res = await _call(coll, "update_one",
                      {"_id": key, f"holds.{hold_id}": {"$exists": True}},
                      {"$inc": {"auto_slots": -1 if h.get("auto") else 0,
                                "total_slots": -1,
                                "risk_usd": -float(h.get("risk_usd") or 0),
                                "rev": 1},
                       "$unset": {f"holds.{hold_id}": ""}})
    return bool(getattr(res, "modified_count", 1))


async def release_reservation(db, trade_or_reservation: dict) -> int:
    """Release the slot/risk held by a trade (or a reservation dict
    returned by reserve_exposure). Idempotent and safe to call on ANY
    close / cancel / reject path, for trades with or without a stored
    reservation (legacy rows use hold id ``trade:<_id>``). Never raises;
    returns the number of scope docs released."""
    if not trade_or_reservation:
        return 0
    t = trade_or_reservation
    if "keys" in t and "id" in t and "status" not in t:   # reservation
        hid, keys = str(t["id"]), list(t.get("keys") or [])
    else:
        hid = _hold_id(t)
        res = t.get("exposure_reservation") or {}
        keys = list((res or {}).get("keys") or [])
        if t.get("account_id"):
            keys.append(account_key(t["account_id"]))
        if t.get("user_id"):
            keys.append(f"user:{t['user_id']}")
    n = 0
    for k in dict.fromkeys(keys):
        try:
            n += 1 if await _release_key(db, k, hid) else 0
        except ReservationStoreUnavailable:
            return 0
        except Exception as e:  # noqa: BLE001 — rebuild heals a miss
            logger.warning("exposure release failed key=%s hold=%s: %s "
                           "(next rebuild heals it)", k, hid, e)
    return n
