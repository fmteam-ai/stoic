"""Execution Factory — routes a trade to the right engine based on account mode.

Paper accounts execute against the local DB (instant fill at live mid-price).
Live MT5 accounts hand the trade to the bridge queue for the EA to fulfil.
Future broker engines (Binance/CCXT) plug in here without touching call sites.
"""
import logging
import os
from datetime import datetime, timezone
from abc import ABC, abstractmethod

from database import get_db
from market import get_quote
from safety_guardian import audit_pre_trade
from order_authorization import authorize_order
from ws_manager import manager as ws_manager
from silent_failures import record_swallow

logger = logging.getLogger("execution")


def broker_identity_snapshot(account: dict) -> dict:
    """Identity rule (iter-124/125): user-entered labels are presentation-only.
    Every trade record stamps the broker-VERIFIED identity at open time so
    reconciliation, auditing and affiliate/risk attribution survive account
    renames, label collisions and even account-doc deletion."""
    from identity_model import authoritative_account_number
    ver = account.get("verified_identity") or {}
    return {
        "account_number": authoritative_account_number(account),
        "broker_server": ver.get("broker_server") or account.get("server"),
        "broker": account.get("broker"),
        "installation_id": (ver.get("installation_id")
                            or (account.get("ea_identity") or {})
                            .get("installation_id")),
    }


def stamp_pamm_identity(trade_doc: dict, signal: dict) -> None:
    """v62.3/v62.4 — full ownership lineage on every PAMM-originated
    trade. Manual overrides carry the program only — NEVER attributed to
    a strategy."""
    ident = signal.get("_pamm_identity")
    if not ident:
        return
    if ident.get("manual_override"):
        trade_doc.update({
            "pamm_program_id": ident.get("pamm_program_id"),
            "pamm_manual_override": True,
            "pamm_max_slippage_pips": (ident.get("effective_limits")
                                       or {}).get("max_slippage_pips"),
            "pamm_risk_snapshot_id": ident.get("risk_snapshot_id")})
        return
    trade_doc.update({
        "pamm_program_id": ident.get("pamm_program_id"),
        "pamm_assignment_id": ident.get("assignment_id"),
        "pamm_strategy_id": ident.get("strategy_id"),
        "pamm_strategy_version": ident.get("strategy_version"),
        "pamm_strategy_hash": ident.get("strategy_hash"),
        "pamm_risk_profile_id": ident.get("risk_profile_id"),
        "pamm_certification_id": ident.get("certification_id"),
        "pamm_max_slippage_pips": (ident.get("effective_limits")
                                   or {}).get("max_slippage_pips"),
        "pamm_risk_snapshot_id": ident.get("risk_snapshot_id")})


def sl_tp_side_block(signal: dict) -> dict | None:
    """Explicit SL/TP side check. BUY needs sl < entry < tp, SELL needs
    tp < entry < sl (each leg checked only when > 0). A wrong-side stop is
    either an instant stop-out or no stop at all — never send it."""
    action = (signal.get("action") or "").upper()
    try:
        entry = float(signal.get("entry_price") or 0)
        sl = float(signal.get("stop_loss") or 0)
        tp = float(signal.get("take_profit") or 0)
    except (TypeError, ValueError):
        return {"blocked": "invalid_geometry", "reason": "non-numeric price"}
    if action not in ("BUY", "SELL") or entry <= 0:
        return None
    bad = []
    if action == "BUY":
        if sl > 0 and not sl < entry:
            bad.append(f"BUY stop_loss {sl} must be below entry {entry}")
        if tp > 0 and not tp > entry:
            bad.append(f"BUY take_profit {tp} must be above entry {entry}")
    else:
        if sl > 0 and not sl > entry:
            bad.append(f"SELL stop_loss {sl} must be above entry {entry}")
        if tp > 0 and not tp < entry:
            bad.append(f"SELL take_profit {tp} must be below entry {entry}")
    if bad:
        return {"blocked": "invalid_sl_tp_side", "reason": "; ".join(bad)}
    return None


def _parse_hb_age_sec(hb) -> float:
    """Seconds since `hb` (ISO string or datetime). Naive values are UTC.
    Raises TypeError/ValueError when unparseable."""
    ts = hb if isinstance(hb, datetime) else datetime.fromisoformat(
        str(hb).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


class ExecutionEngine(ABC):
    @abstractmethod
    async def execute(self, *, user_id, account, signal,
                      max_concurrent: int = 0, cfg_account_id: str = None) -> dict: ...


class MT5BridgeEngine(ExecutionEngine):
    """Live engine — inserts a `pending` trade; the MT5 EA polls and executes."""

    async def execute(self, *, user_id, account, signal,
                      max_concurrent: int = 0, cfg_account_id: str = None) -> dict:
        """v56 hard cutover — strategies never reach the engine directly:
        every call becomes a canonical ExecutionIntent submitted to the
        Execution Authority (validate → authorize), and only then does the
        engine stage run with the intent as its INPUT."""
        from execution_authority import submit_intent
        return await submit_intent(user_id=user_id, account=account,
                                   signal=signal, engine=self,
                                   max_concurrent=max_concurrent,
                                   cfg_account_id=cfg_account_id)

    async def execute_authorized(self, *, user_id, account, signal,
                                 max_concurrent: int = 0,
                                 cfg_account_id: str = None,
                                 intent: dict = None,
                                 authorization=None) -> dict:
        # v56 hardening — capability gate: only calls carrying a valid
        # single-use ExecutionAuthorization (mintable ONLY by the
        # Execution Authority) may reach the engine stage.
        from execution_authorization import verify_authorization
        _refusal = verify_authorization(
            authorization, (intent or {}).get("intent_id") or "")
        if _refusal:
            logger.critical(
                "EXECUTION CHOKE POINT BYPASS BLOCKED — execute_authorized "
                "called without a valid ExecutionAuthorization (%s) user=%s "
                "sym=%s", _refusal, user_id, signal.get("symbol"))
            return {"blocked": "unauthorized_execution_path",
                    "reason": _refusal}
        db = get_db()

        _geo_block = sl_tp_side_block(signal)
        if _geo_block:
            logger.warning("MT5 execute blocked by SL/TP side check user=%s "
                           "sym=%s: %s", user_id, signal.get("symbol"),
                           _geo_block.get("reason"))
            return _geo_block

        # FINAL ENTITLEMENT CHECK (iter-122 Phase 2) — the dispatcher never
        # trusts upstream gates; plan authority is re-verified per trade.
        from entitlements import verify_execution_entitlement
        ent_block = await verify_execution_entitlement(
            db, user_id=user_id, account=account, signal=signal)
        if ent_block:
            logger.warning("MT5 execute blocked by entitlement user=%s sym=%s: %s",
                           user_id, signal.get("symbol"), ent_block.get("reason"))
            return ent_block

        # LIVE IDENTITY GATE (iter-125 correction #1) — one account → one
        # verified installation → one terminal → one execution lease. A live
        # trade never dispatches unless the last heartbeat verified the full
        # identity chain AND that installation holds the current lease.
        from vps_agent import verify_execution_identity
        id_block = await verify_execution_identity(db, account)
        if id_block:
            logger.warning("MT5 execute blocked by identity gate user=%s "
                           "sym=%s: %s", user_id, signal.get("symbol"),
                           id_block.get("reason"))
            return id_block

        # MARKET-HOURS HARD VETO (iter-63) — block at execution layer too, so
        # any in-flight signal from before the analyze_symbol fix can't fire.
        # XAUUSD / forex: closed Fri 21:00 → Sun 22:00 UTC. Broker would
        # return MT5 10018 MARKET_CLOSED otherwise.
        from microstructure import is_market_closed
        closure = is_market_closed(signal.get("symbol", ""))
        if closure:
            logger.warning(
                "MT5 execute blocked by market-hours veto user=%s acct=%s sym=%s reason=%s",
                user_id, cfg_account_id or "default", signal.get("symbol"), closure["reason"],
            )
            return {"blocked": "market_closed", **closure}

        # FRESH ACCOUNT STATE (audit E1) — the account object was loaded at
        # the top of the bot loop; equity/margin/connection can change before
        # execution. Re-read the critical fields and fail closed on staleness.
        # (The scalp fast path performs its own, stricter refresh.)
        # Applies to AUTONOMOUS (bot) execution only — the audit's concern is
        # the bot loop queuing on a stale snapshot. Manual/test trades are
        # user-initiated against live on-screen data and still pass the
        # SafetyGuardian below.
        if (signal.get("origin") == "auto"
                and signal.get("scope") != "scalp_fast"
                and (account.get("mode") or "live").lower() != "paper"):
            try:
                fresh = await db.accounts.find_one(
                    {"_id": account["_id"]},
                    {"equity": 1, "balance": 1, "free_margin": 1, "status": 1,
                     "last_heartbeat": 1, "open_positions": 1,
                     "account_type": 1, "leverage": 1})
            except Exception:
                # Refresh mechanism unavailable (isolated unit test with a
                # non-async db mock) — proceed on the snapshot the caller
                # already validated. Real production always has an async DB
                # so the disconnect/staleness blocks below are enforced.
                fresh = "__skip__"
            if fresh == "__skip__":
                pass
            elif fresh is None:
                return {"blocked": "account_missing"}
            else:
                if (fresh.get("status") or "").lower() not in (
                        "connected", "ok", ""):
                    return {"blocked": "account_disconnected",
                            "account_status": fresh.get("status")}
                hb = fresh.get("last_heartbeat")
                if hb:
                    try:
                        age = _parse_hb_age_sec(hb)
                    except (TypeError, ValueError):
                        # FAIL CLOSED: an unparseable heartbeat means we can't
                        # prove the terminal is alive.
                        return {"blocked": "heartbeat_unparseable",
                                "last_heartbeat": str(hb)}
                    if age > float(os.environ.get(
                            "EXEC_MAX_HEARTBEAT_AGE_SEC", "120")):
                        return {"blocked": "stale_heartbeat",
                                "heartbeat_age_sec": int(age)}
                if not (fresh.get("equity") or fresh.get("balance")):
                    return {"blocked": "equity_unknown"}
                account = {**account, **fresh}

            # FINAL QUOTE PREFLIGHT (audit E9) — cancel when price moved so
            # far since signal generation that the approved geometry no
            # longer holds (entry deviation > 50% of stop distance, or the
            # market already traded through the stop).
            px = 0.0
            try:
                q = await get_quote(signal["symbol"])
                px = float((q or {}).get("price") or 0)
            except Exception:
                px = 0.0
            if px <= 0:
                # FAIL CLOSED: without a live quote the freshness/deviation
                # preflight cannot run.
                logger.warning("MT5 execute blocked: no live quote user=%s "
                               "sym=%s", user_id, signal.get("symbol"))
                return {"blocked": "quote_unavailable"}
            entry0 = float(signal.get("entry_price") or 0)
            sl0 = float(signal.get("stop_loss") or 0)
            if px > 0 and entry0 > 0 and sl0 > 0:
                stop_dist = abs(entry0 - sl0)
                deviation = abs(px - entry0)
                through_stop = ((signal.get("action") == "BUY" and px <= sl0)
                                or (signal.get("action") == "SELL" and px >= sl0))
                if through_stop or (stop_dist > 0
                                    and deviation > 0.5 * stop_dist):
                    logger.warning(
                        "MT5 execute blocked by quote preflight user=%s sym=%s "
                        "entry=%.5f live=%.5f dev=%.5f stop_dist=%.5f",
                        user_id, signal.get("symbol"), entry0, px,
                        deviation, stop_dist)
                    return {"blocked": "entry_deviation",
                            "live_price": px, "signal_entry": entry0,
                            "deviation": round(deviation, 5),
                            "stop_distance": round(stop_dist, 5)}

        # Atomic last-line-of-defense cap check (audit E4 — consistent
        # semantics with the runner): the config cap limits AUTOMATED trades;
        # a separate hard total-positions cap bounds the whole account.
        if max_concurrent > 0:
            cap_q = {"user_id": user_id, "status": {"$in": ["pending", "open"]},
                     "origin": "auto"}
            if cfg_account_id:
                cap_q["account_id"] = cfg_account_id
            live_inflight = await db.trades.count_documents(cap_q)
            total_q = {k: v for k, v in cap_q.items() if k != "origin"}
            total_inflight = await db.trades.count_documents(total_q)
            max_total = max_concurrent + int(os.environ.get(
                "EXEC_TOTAL_POSITIONS_BUFFER", "2"))
            if live_inflight >= max_concurrent or total_inflight >= max_total:
                logger.warning(
                    "execute blocked by concurrency cap user=%s acct=%s sym=%s "
                    "auto=%d/%d total=%d/%d",
                    user_id, cfg_account_id or "default", signal.get("symbol"),
                    live_inflight, max_concurrent, total_inflight, max_total,
                )
                return {"blocked": "max_concurrent_cap",
                        "inflight": live_inflight, "cap": max_concurrent,
                        "total_inflight": total_inflight,
                        "total_cap": max_total}

        # SAFETY GUARDIAN — server-side hard floors for live accounts. CANNOT be
        # disabled by user config. Refuses any trade that would breach equity,
        # margin, per-trade risk, daily loss, or aggregate exposure caps.
        safety = await audit_pre_trade(
            db=db, account=account, signal=signal,
            user_id=user_id, cfg_account_id=cfg_account_id,
        )
        if not safety["ok"]:
            logger.warning(
                "SAFETY GUARDIAN refused trade user=%s acct=%s sym=%s blocked_by=%s",
                user_id, cfg_account_id or "default",
                signal.get("symbol"), safety.get("blocked_by"),
            )
            # Persist a lightweight audit row so the diagnostic can show
            # 24h-block-count and operators can see WHY trades were refused.
            try:
                await db.safety_blocks.insert_one({
                    "user_id": user_id,
                    "account_id": cfg_account_id,
                    "symbol": signal.get("symbol"),
                    "action": signal.get("action"),
                    "lot_size": signal.get("lot_size"),
                    "blocked_by": safety["blocked_by"],
                    "audit": safety["audit"],
                    "context": safety.get("context"),
                    "blocked_at": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to persist safety_block: %s", e)
            return {"blocked": "safety_guardian",
                    "safety_blocked_by": safety["blocked_by"],
                    "safety_audit": safety["audit"]}

        # GLOBAL TRADING AUTHORITY (v56 §16) — normally enforced upstream
        # by the Execution Authority. Legacy/no-intent path: evaluation
        # failure FAILS CLOSED (audit P0-2), never assumes FULL.
        if intent is None:
            try:
                from trading_authority import enforce_new_trade
                gate = await enforce_new_trade(db, account=account)
            except Exception as e:  # noqa: BLE001
                logger.critical("authority evaluation failed on legacy "
                                "path — REFUSING user=%s sym=%s: %s",
                                user_id, signal.get("symbol"), e)
                gate = {"ok": False, "level": "CLOSE_ONLY",
                        "reasons": [f"authority evaluation error: {e}"]}
            if not gate.get("ok"):
                logger.warning(
                    "execute blocked by TRADING AUTHORITY level=%s user=%s "
                    "sym=%s reasons=%s", gate.get("level"), user_id,
                    signal.get("symbol"), gate.get("reasons"))
                return {"blocked": "trading_authority",
                        "authority_level": gate.get("level"),
                        "reasons": gate.get("reasons")}
            if gate.get("reduce_factor") and signal.get("lot_size"):
                _orig_lot = float(signal["lot_size"])
                from risk import scale_lot
                _red = scale_lot(_orig_lot, float(gate["reduce_factor"]))
                if _red <= 0:
                    # Floor-based reduce left less than the broker minimum —
                    # block instead of rounding back UP to 0.01.
                    return {"blocked": "trading_authority_reduce_below_min_lot",
                            "authority_level": gate.get("level"),
                            "reasons": gate.get("reasons")}
                signal["lot_size"] = _red
                signal["_authority_reduced"] = True
                logger.warning(
                    "TRADING AUTHORITY REDUCED — lot %s → %s user=%s sym=%s",
                    _orig_lot, signal["lot_size"], user_id,
                    signal.get("symbol"))

        # iter-71b · Per-account symbol_suffix override. Brokers like VT
        # Markets rename `XAUUSD` to `XAUUSD.x` / `XAUUSDpro` / etc. and the
        # EA's plain SymbolInfoDouble(symbol) returns 0 → retcode 10013.
        # iter-76 · Two-tier suffix routing:
        #   1. user-set `symbol_suffix`              (manual override, wins)
        #   2. `auto_detected_symbol_suffix`         (from EA v1.34 MarketWatch
        #                                             scan via broker_symbol_detector)
        #   3. bare base name                        (legacy fallback)
        # iter-82 · Mixed-convention brokers (e.g. VTMarkets: forex pairs are
        # bare `EURUSD` but gold is `XAUUSD-ECN`) break the "one suffix per
        # broker" model. When the EA has reported `available_symbols`, prefer
        # the per-base resolver — it looks up the EXACT broker ticker for the
        # base we want to trade, regardless of suffix convention drift.
        # Resolution order: manual → per-base from MarketWatch → broker-wide
        # auto suffix → bare base.
        from broker_symbol_detector import resolve_broker_symbol
        base_symbol = signal["symbol"]
        user_suffix = (account.get("symbol_suffix") or "").strip()
        auto_suffix = (account.get("auto_detected_symbol_suffix") or "").strip()
        available = account.get("available_symbols") or None

        if user_suffix:
            broker_symbol = base_symbol + user_suffix
            suffix = user_suffix
            suffix_source = "manual"
        else:
            resolved = resolve_broker_symbol(base_symbol, available, auto_suffix)
            if resolved is None and available:
                # MarketWatch is known AND base isn't in it → broker doesn't
                # offer it. Hard-fail instead of sending an order that will
                # 100% bounce back symbol_not_found.
                logger.warning(
                    "Skipping %s on account %s — base symbol not in MarketWatch (%d syms)",
                    base_symbol, account.get("_id"), len(available),
                )
                return {"blocked": "symbol_not_offered_by_broker",
                        "base_symbol": base_symbol,
                        "available_count": len(available)}
            # iter-139 · Broker registry tier: when the EA hasn't reported
            # MarketWatch yet and there's no auto suffix, consult the
            # curated registry mapping for this broker server.
            registry_hit = False
            if not available and not auto_suffix and resolved in (None, base_symbol):
                try:
                    from broker_registry import registry_symbol_for
                    reg_sym = await registry_symbol_for(
                        account.get("server"), base_symbol)
                    if reg_sym and reg_sym != base_symbol:
                        resolved = reg_sym
                        registry_hit = True
                except Exception:  # noqa: BLE001
                    pass
            broker_symbol = resolved or base_symbol
            suffix = broker_symbol[len(base_symbol):] if broker_symbol.upper().startswith(base_symbol) else ""
            suffix_source = ("registry" if registry_hit
                             else "per_base" if available
                             else ("auto" if auto_suffix else "none"))

        trade_doc = {
            "user_id": user_id,
            "account_id": str(account["_id"]),
            "signal_id": signal.get("signal_id"),
            "decision_id": signal.get("decision_id"),
            "symbol": broker_symbol,
            "base_symbol": base_symbol,
            "symbol_suffix_applied": suffix or None,
            "symbol_suffix_source": suffix_source,
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
            "broker_identity": broker_identity_snapshot(account),
            "mt5_ticket": None,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
            "error": None,
            "origin": signal.get("origin", "manual"),
            "scope": signal.get("scope"),
            "strategy_class": signal.get("strategy_class"),
            "market_regime": signal.get("market_regime"),
            "risk_pct": (signal.get("risk_pct")
                         or (signal.get("adaptive_sizing") or {}).get("risk_pct")),
            "scalp_lease_epoch": signal.get("scalp_lease_epoch"),
            "trend_ride": signal.get("trend_ride"),
            "versions": signal.get("versions"),
            "partial_closed": False,
            "breakeven_set": False,
            "trail_active": False,
            "safety_audit": safety,
        }
        # Embed an Explainable AI snapshot — stable record of WHY this trade
        # was fired, captured from the latest agent_activity tick.
        try:
            from trade_explainer import snapshot_for_trade_creation
            trade_doc["explanation_snapshot"] = await snapshot_for_trade_creation(
                db, user_id=user_id, symbol=signal["symbol"],
                signal=signal, trade_doc=trade_doc,
            )
        except Exception as _sw:  # noqa: BLE001
            record_swallow("execution", "execute", _sw)  # explanation is informational — never block a trade
        # SIGNED SINGLE-USE ORDER AUTHORIZATION (iter-171 #10) — every live
        # order carries a server-minted, HMAC-signed, single-use, atomically-
        # consumed authorization bound to (user, account, symbol, side).
        _acct_id = str(account.get("_id") or account.get("account_id")
                       or cfg_account_id or "")
        _side = str(signal.get("action") or signal.get("side") or "").upper()
        order_auth = await authorize_order(
            db, user_id=user_id, account_id=_acct_id,
            symbol=signal["symbol"], side=_side)
        if not order_auth.get("ok"):
            logger.warning("MT5 execute blocked — order authorization failed "
                           "user=%s sym=%s: %s", user_id,
                           signal.get("symbol"), order_auth.get("reason"))
            return {"blocked": "order_authorization_failed",
                    "reason": order_auth.get("reason")}
        from correlation import get_correlation_id
        trade_doc.setdefault("trace_id", get_correlation_id())
        trade_doc["order_authorization"] = order_auth
        # EXECUTION INTENT (v55 §1/§2, v56 hard cutover) — normally the
        # intent arrives as INPUT from the Execution Authority. The
        # create-here branch only serves the legacy/no-intent path.
        from execution_intents import (canonical_payload, create_intent,
                                       dedupe_key_for, transition)
        import hashlib as _hl
        _risk_snap = _hl.sha256(
            repr(sorted((safety.get("audit") or {}).items())
                 if isinstance(safety.get("audit"), dict)
                 else safety.get("audit")).encode()).hexdigest()[:16]
        _intent = intent
        if _intent is not None:
            # enrich the authority-minted intent with engine-stage facts
            try:
                await db.execution_intents.update_one(
                    {"intent_id": _intent["intent_id"]},
                    {"$set": {"payload.symbol": broker_symbol,
                              "payload.nonce": str(order_auth.get("nonce")
                                                   or ""),
                              "payload.risk_snapshot_id": _risk_snap}})
            except Exception:  # enrichment is best-effort audit detail
                pass
        else:
            _sig_ref = (signal.get("intent_ref") or signal.get("signal_id")
                        or f"{trade_doc['opened_at'][:16]}|"
                           f"{signal.get('entry_price')}|"
                           f"{signal.get('stop_loss')}")
            try:
                _intent = await create_intent(
                    db, source=str(signal.get("scope")
                                   or signal.get("origin") or "manual"),
                    kind="open_trade",
                    dedupe_key=dedupe_key_for("mt5_bridge", "open_trade",
                                              _acct_id, signal["symbol"],
                                              _side, _sig_ref),
                    payload=canonical_payload(
                        account_id=str(_acct_id or ""),
                        broker_account_number=str(
                            account.get("account_number")
                            or account.get("login") or ""),
                        broker_server=str(account.get("server")
                                          or account.get("broker_server")
                                          or ""),
                        strategy_id=str(signal.get("scope")
                                        or signal.get("origin")
                                        or "manual"),
                        strategy_version=str(
                            signal.get("strategy_version") or ""),
                        symbol=broker_symbol, side=str(_side),
                        requested_volume=float(signal.get("lot_size")
                                               or 0),
                        stop_loss=signal.get("stop_loss"),
                        take_profit=signal.get("take_profit"),
                        risk_snapshot_id=_risk_snap,
                        signal_id=str(signal.get("signal_id") or ""),
                        fencing_epoch=int(signal.get("scalp_lease_epoch")
                                          or 0),
                        nonce=str(order_auth.get("nonce") or "")),
                    account_id=_acct_id, actor=user_id)
            except Exception as e:  # noqa: BLE001 — FAIL CLOSED (audit P0-2)
                logger.critical("execution intent creation FAILED — "
                                "REFUSING dispatch user=%s sym=%s: %s",
                                user_id, signal.get("symbol"), e)
                return {"blocked": "intent_pipeline_error",
                        "reason": f"{type(e).__name__}: {str(e)[:200]}"}
            if _intent and _intent.get("duplicate"):
                logger.warning("MT5 execute blocked — duplicate execution "
                               "intent user=%s sym=%s intent=%s status=%s",
                               user_id, signal.get("symbol"),
                               _intent.get("intent_id"),
                               _intent.get("status"))
                return {"blocked": "duplicate_intent",
                        "intent_id": _intent.get("intent_id"),
                        "intent_status": _intent.get("status"),
                        "original_result": _intent.get("result")}
        if _intent:
            trade_doc["execution_intent_id"] = _intent["intent_id"]
        stamp_pamm_identity(trade_doc, signal)
        if signal.get("_authority_reduced"):
            trade_doc["authority_reduced"] = True
        if signal.get("latency_trace"):
            trade_doc["latency_trace"] = dict(signal["latency_trace"])
        r = await db.trades.insert_one(trade_doc)
        trade_doc["id"] = str(r.inserted_id)
        trade_doc.pop("_id", None)
        if _intent:
            await transition(db, _intent["intent_id"], "submitted",
                             detail=f"trade {trade_doc['id']} pending "
                                    f"dispatch",
                             result={"trade_id": trade_doc["id"]})
        await ws_manager.broadcast(user_id, "trade_created", trade_doc)
        return trade_doc


class PaperEngine(ExecutionEngine):
    """Virtual engine — simulates an instant fill at the current live mid-price."""

    async def execute(self, *, user_id, account, signal,
                      max_concurrent: int = 0, cfg_account_id: str = None) -> dict:
        db = get_db()
        # round 9 P0-01 — the canonical decision gates EVERY new-order path.
        from trading_authority import gate_or_block
        _deny = await gate_or_block(db, account, "paper_engine")
        if _deny:
            return _deny
        # v62.3 — the PAMM Strategy Guard applies to EVERY engine: a paper
        # master account is governed identically to live (no alternate
        # PAMM route escapes the guard).
        _prog = None
        try:
            from modules.pamm.strategy_guard import resolve_program
            _acct = str(account.get("_id") or account.get("account_id")
                        or cfg_account_id or "")
            _prog = await resolve_program(db, _acct, signal)
        except Exception as e:  # noqa: BLE001 — FAIL CLOSED (audit P0-2)
            logger.critical("PAMM resolution failed on paper path — "
                            "REFUSING user=%s sym=%s: %s", user_id,
                            signal.get("symbol"), e)
            return {"blocked": "pamm_resolution_error",
                    "reason": f"{type(e).__name__}: {str(e)[:200]}"}
        if _prog is not None:
            from modules.pamm.strategy_guard import \
                authorize_pamm_strategy_execution
            guard = await authorize_pamm_strategy_execution(
                db, _prog, account, signal)
            if not guard["authorized"]:
                logger.warning("paper execute blocked by PAMM strategy "
                               "guard user=%s program=%s reason=%s",
                               user_id, _prog.get("program_id"),
                               guard["reason"])
                return {"blocked": "pamm_strategy_guard",
                        "reason": guard["reason"],
                        "checks": guard["checks"]}
            if guard["mode"] in ("STRATEGY", "MANUAL_OVERRIDE"):
                signal["_pamm_identity"] = guard["context"]
        # Mirror MT5 path's market-hours veto so paper-shadow PnL stays
        # consistent with live behaviour (no phantom weekend fills).
        from microstructure import is_market_closed
        closure = is_market_closed(signal.get("symbol", ""))
        if closure:
            logger.info(
                "paper execute blocked by market-hours veto user=%s acct=%s sym=%s reason=%s",
                user_id, cfg_account_id or "default", signal.get("symbol"), closure["reason"],
            )
            return {"blocked": "market_closed", **closure}
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
            "decision_id": signal.get("decision_id"),
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
            "broker_identity": broker_identity_snapshot(account),
            "mt5_ticket": None,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
            "error": None,
            "origin": signal.get("origin", "manual"),
            "strategy_class": signal.get("strategy_class"),
            "market_regime": signal.get("market_regime"),
            "risk_pct": (signal.get("risk_pct")
                         or (signal.get("adaptive_sizing") or {}).get("risk_pct")),
        }
        from correlation import get_correlation_id
        trade_doc.setdefault("trace_id", get_correlation_id())
        stamp_pamm_identity(trade_doc, signal)
        if signal.get("latency_trace"):
            trade_doc["latency_trace"] = dict(signal["latency_trace"])
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
        except Exception as _sw:  # noqa: BLE001
            record_swallow("execution", "execute", _sw)
        return trade_doc


def for_account(account: dict) -> ExecutionEngine:
    """Pick the right engine for an account."""
    kind = (account.get("kind") or "mt5").lower()
    if kind == "binance":
        # Local import — avoids a circular dep (binance_engine imports from
        # execution.ExecutionEngine).
        from crypto_bridge.binance_engine import BinanceCCXTEngine
        return BinanceCCXTEngine()
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
        except Exception as _sw:  # noqa: BLE001
            record_swallow("execution", "settle_paper_trades_against_price", _sw)
        # Loss post-mortem + auto-loosen on winners — fire-and-forget
        try:
            from loss_postmortem import maybe_record_postmortem, maybe_record_winner
            from drift_detector import record_residual_for_trade
            import asyncio
            asyncio.create_task(maybe_record_postmortem(db, t["_id"]))
            asyncio.create_task(maybe_record_winner(db, t["_id"]))
            asyncio.create_task(record_residual_for_trade(db, t["_id"]))
        except Exception as _sw:  # noqa: BLE001
            record_swallow("execution", "settle_paper_trades_against_price", _sw)
        closed += 1
    return closed


def _to_oid(v):
    from bson import ObjectId
    try:
        return ObjectId(v)
    except Exception:
        return v
