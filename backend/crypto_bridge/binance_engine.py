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
  • Every filled entry is immediately protected by an EXCHANGE-SIDE stop
    (+ take-profit; native OCO on Binance spot / OKX) — see
    crypto_bridge/protective.py. If the stop cannot be placed the position
    is flattened at market and the trade closed with
    close_reason="protective_order_failed". Limit entries are protected
    by the lifecycle sweep (crypto_lifecycle.py) once they fill.
  • The bot/manual entry goes through the Execution Authority
    (execution_authority.submit_intent → execute_authorized) exactly like
    MT5: enablement + role locks, canonical intent + dedupe, Global
    Trading Authority, single-use ExecutionAuthorization.
  • Defence-in-depth: any account that hasn't been explicitly flipped to
    `live=true` AND `BINANCE_LIVE_ENABLED=true` runs against the sandbox.
"""
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone

from bson import ObjectId

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


def _post_dispatch_uncertain(exc: BaseException) -> bool:
    """True when an order submission failed in a way where the order MAY
    have reached the exchange (timeout / dropped connection) — the outcome
    must then be resolved from exchange truth, never assumed.
      • ccxt ExchangeError subclasses (InsufficientFunds, InvalidOrder,
        AuthenticationError…) are PARSED exchange answers → not placed.
      • other ccxt errors (NetworkError, RequestTimeout, OperationFailed)
        and raw timeouts / connection drops → uncertain.
      • anything else is raised by our own code before the request is
        built (decrypt, validation, refusal guards) → not placed."""
    import asyncio
    try:
        import ccxt.async_support as _ccxt  # type: ignore[import]
        if isinstance(exc, _ccxt.ExchangeError):
            return False
        if isinstance(exc, _ccxt.BaseError):
            return True
    except Exception:  # noqa: BLE001
        pass
    if isinstance(exc, ConnectionRefusedError):
        return False
    return isinstance(exc, (TimeoutError, asyncio.TimeoutError, ConnectionError))


async def _intent_transition(db, intent: dict | None, to: str, detail: str = "",
                             result: dict | None = None) -> None:
    if not intent or not intent.get("intent_id"):
        return
    try:
        from execution_intents import transition
        await transition(db, intent["intent_id"], to, detail=detail, result=result)
    except Exception as e:  # noqa: BLE001 — audit trail must not block
        logger.warning("intent %s transition → %s failed: %s",
                       intent.get("intent_id"), to, e)


class BinanceCCXTEngine(ExecutionEngine):
    """Live engine for Binance Spot accounts via CCXT."""

    async def execute(self, *, user_id, account, signal,
                      max_concurrent: int = 0, cfg_account_id: str = None) -> dict:
        """Crypto is not a bypass of the execution choke point: every call
        becomes a canonical ExecutionIntent submitted to the Execution
        Authority (enablement/role locks → intent + dedupe → validation →
        Global Trading Authority), which then calls execute_authorized()
        with a single-use ExecutionAuthorization."""
        from execution_authority import submit_intent
        if signal.get("lot_size") is not None:
            # remember the requested base amount — the authority's REDUCED
            # branch rounds lots for MT5 (max(0.01, round(x, 2))), which for
            # a 0.001 BTC order would be a 10× INCREASE.
            signal.setdefault("_crypto_requested_amount", float(signal["lot_size"] or 0))
        return await submit_intent(user_id=user_id, account=account,
                                   signal=signal, engine=self,
                                   max_concurrent=max_concurrent,
                                   cfg_account_id=cfg_account_id)

    async def execute_authorized(self, *, user_id, account, signal,
                                 max_concurrent: int = 0,
                                 cfg_account_id: str = None,
                                 intent: dict = None,
                                 authorization=None) -> dict:
        from execution_authorization import verify_authorization
        _refusal = verify_authorization(
            authorization, (intent or {}).get("intent_id") or "")
        if _refusal:
            logger.critical(
                "EXECUTION CHOKE POINT BYPASS BLOCKED — crypto execute_authorized "
                "called without a valid ExecutionAuthorization (%s) user=%s sym=%s",
                _refusal, user_id, signal.get("symbol"))
            return {"blocked": "unauthorized_execution_path", "reason": _refusal}
        db = get_db()
        # round 9 P0-01 — canonical trading decision: crypto is not a bypass.
        from trading_authority import gate_or_block
        _deny = await gate_or_block(db, account, "binance_engine")
        if _deny:
            return _deny

        # SL/TP geometry + mandatory stop: an exchange-side stop is placed
        # right after the fill, so a crypto entry without a valid stop is
        # refused before anything leaves STOIC.
        from execution import sl_tp_side_block
        _geo = sl_tp_side_block(signal)
        if _geo:
            return _geo
        try:
            if float(signal.get("stop_loss") or 0) <= 0:
                return {"blocked": "crypto_stop_required",
                        "reason": "crypto entries require a stop_loss (an "
                                  "exchange-side stop is placed on fill)"}
        except (TypeError, ValueError):
            return {"blocked": "invalid_geometry", "reason": "non-numeric stop_loss"}

        # FINAL ENTITLEMENT CHECK (iter-122 Phase 2) — same authority
        # boundary as the MT5 engine; crypto is not a bypass.
        from entitlements import verify_execution_entitlement
        ent_block = await verify_execution_entitlement(
            db, user_id=user_id, account=account, signal=signal)
        if ent_block:
            logger.warning("binance execute blocked by entitlement user=%s sym=%s: %s",
                           user_id, signal.get("symbol"), ent_block.get("reason"))
            return ent_block

        # Real-money gates on the BOT path too (previously only the manual
        # route checked BINANCE_LIVE_ENABLED, and no-sandbox exchanges sent
        # "testnet" orders to the live venue).
        from crypto_bridge.ccxt_engine import orders_blocked_reason
        _ob = orders_blocked_reason(account)
        if _ob:
            logger.warning("binance execute blocked user=%s sym=%s: %s",
                           user_id, signal.get("symbol"), _ob)
            return {"blocked": "crypto_live_gate", "reason": _ob}

        symbol_internal = signal["symbol"]
        ccxt_symbol = normalize_symbol(symbol_internal)
        action = (signal.get("action") or "").upper()
        side = "buy" if action == "BUY" else "sell"
        amount = float(signal.get("lot_size") or 0)
        if signal.get("_authority_reduced"):
            # Authority REDUCED → half the REQUESTED base amount (see execute()).
            _req = float(signal.get("_crypto_requested_amount")
                         or ((intent or {}).get("payload") or {}).get("requested_volume")
                         or 0)
            if _req > 0:
                amount = min(amount, _req * 0.5) if amount > 0 else _req * 0.5

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
        # Per-account override (bot_config.crypto_risk_pct_per_trade) takes
        # precedence over the global env CRYPTO_MAX_RISK_PCT_PER_TRADE.
        per_account_cap = None
        if cfg_account_id:
            try:
                cfg_doc = await db.bot_configs.find_one({
                    "user_id": user_id, "account_id": cfg_account_id,
                })
                if cfg_doc and cfg_doc.get("crypto_risk_pct_per_trade") is not None:
                    per_account_cap = float(cfg_doc["crypto_risk_pct_per_trade"])
            except Exception:
                per_account_cap = None
        cap_pct_used = per_account_cap if per_account_cap is not None else _crypto_risk_cap_pct()

        equity = float(account.get("equity") or account.get("balance") or 0)
        entry_px = float(signal.get("entry_price") or 0)
        sl_px = float(signal.get("stop_loss") or 0)
        risk_per_unit = abs(entry_px - sl_px) if entry_px > 0 and sl_px > 0 else 0
        crypto_risk_usd = risk_per_unit * amount
        crypto_cap = equity * (cap_pct_used / 100.0)
        if equity > 0 and crypto_risk_usd > crypto_cap and crypto_cap > 0:
            logger.warning(
                "BINANCE per-trade risk cap exceeded user=%s risk=$%.2f cap=$%.2f (%.2f%%)",
                user_id, crypto_risk_usd, crypto_cap, cap_pct_used,
            )
            return {"blocked": "crypto_risk_cap",
                    "risk_usd": crypto_risk_usd, "cap_usd": crypto_cap,
                    "cap_pct_used": cap_pct_used,
                    "cap_source": "per_account" if per_account_cap is not None else "env_default"}

        _acct_id = str(account.get("_id") or account.get("account_id")
                       or cfg_account_id or "")

        # 3b. ATOMIC EXPOSURE RESERVATION (execution_authority) — the count
        # gate above is a cheap pre-filter; this is the race-free authority
        # for slots + open stop risk, reserved BEFORE anything leaves STOIC.
        # Crypto amounts are base-asset units, not lots: the stop risk is
        # amount × |entry − stop| in quote currency (stop is mandatory).
        rsv = await self._reserve_exposure(
            db, user_id=user_id, account=account, acct_id=_acct_id,
            cfg_account_id=cfg_account_id, symbol=symbol_internal,
            amount=amount, risk_usd=crypto_risk_usd, safety=safety,
            origin=signal.get("origin"), max_concurrent=max_concurrent)
        if not rsv.get("ok"):
            logger.warning(
                "binance execute blocked by atomic exposure reservation "
                "user=%s sym=%s: %s", user_id, ccxt_symbol, rsv.get("blocked"))
            return {k: v for k, v in rsv.items() if k != "ok"}
        reservation = rsv.get("reservation")

        # 4. Signed single-use order authorization (same as MT5).
        from order_authorization import authorize_order
        order_auth = await authorize_order(
            db, user_id=user_id, account_id=_acct_id,
            symbol=symbol_internal, side=action)
        if not order_auth.get("ok"):
            await self._release(db, reservation)
            logger.warning("crypto execute blocked — order authorization failed "
                           "user=%s sym=%s: %s", user_id, symbol_internal,
                           order_auth.get("reason"))
            return {"blocked": "order_authorization_failed",
                    "reason": order_auth.get("reason")}

        # 5. Smart-routed order.
        order_type, limit_price = _smart_route(signal)
        order_resp: dict = {}
        trade_oid = ObjectId()
        # Deterministic client order id (alphanumeric, ≤32 chars for OKX) so
        # a retried submission of the same signal can't double-fill. Falls
        # back to the pre-generated trade id so EVERY entry is resolvable by
        # clientOrderId after a lost response.
        _cid_src = str(signal.get("signal_id") or signal.get("decision_id") or "")
        _cid_src = "".join(ch for ch in _cid_src if ch.isalnum()) or str(trade_oid)
        client_order_id = f"stoic{_cid_src}"[:32]
        entry_uncertain = None
        protect_result = None
        trade_doc: dict = {}
        inserted = False
        try:
            async with BinanceClient(account) as client:
                try:
                    if order_type == "limit":
                        order_resp = await client.create_limit_order(
                            ccxt_symbol, side, amount, limit_price,
                            client_order_id=client_order_id,
                        )
                    else:
                        order_resp = await client.create_market_order(
                            ccxt_symbol, side, amount,
                            client_order_id=client_order_id,
                        )
                except Exception as e:  # noqa: BLE001
                    if not _post_dispatch_uncertain(e):
                        raise
                    # The order MAY be live on the exchange: persist a
                    # pending trade keyed by its clientOrderId — the
                    # lifecycle sweep resolves it from exchange truth.
                    # Never report "blocked" (that releases the dedupe key).
                    logger.critical("crypto entry outcome UNKNOWN user=%s sym=%s "
                                    "cid=%s: %s", user_id, ccxt_symbol,
                                    client_order_id, e)
                    entry_uncertain = f"{type(e).__name__}: {str(e)[:200]}"
                    order_resp = {}
                trade_doc = self._build_trade_doc(
                    trade_oid=trade_oid, user_id=user_id, account=account,
                    signal=signal, symbol_internal=symbol_internal,
                    ccxt_symbol=ccxt_symbol, action=action, amount=amount,
                    entry_px=entry_px, sl_px=sl_px, order_type=order_type,
                    order_resp=order_resp, client_order_id=client_order_id,
                    safety=safety, crypto_risk_usd=crypto_risk_usd,
                    crypto_cap=crypto_cap, cap_pct_used=cap_pct_used,
                    per_account_cap=per_account_cap,
                    entry_uncertain=entry_uncertain)
                trade_doc["order_authorization"] = order_auth
                if intent:
                    trade_doc["execution_intent_id"] = intent.get("intent_id")
                if reservation:
                    trade_doc["exposure_reservation"] = reservation
                await self._enrich_and_insert(db, user_id, symbol_internal,
                                              signal, trade_doc)
                inserted = True
                await _intent_transition(
                    db, intent, "submitted",
                    detail=(f"crypto order {trade_doc['exchange_order_id'] or client_order_id} "
                            f"{'UNKNOWN' if entry_uncertain else order_resp.get('status')}"),
                    result={"trade_id": str(trade_oid)})
                # 6. Exchange-side protection immediately after a fill.
                if trade_doc["status"] == "open":
                    await _intent_transition(db, intent, "filled",
                                             detail="market entry filled",
                                             result={"trade_id": str(trade_oid)})
                    from crypto_bridge.protective import protect_or_flatten
                    protect_result = await protect_or_flatten(
                        db, client, account, trade_doc, entry_order=order_resp)
        except Exception as e:  # noqa: BLE001
            if not inserted:
                # no trade row exists → nothing will ever release this hold
                await self._release(db, reservation)
            if not trade_doc.get("_id"):
                logger.exception("Binance order placement failed: %s", e)
                return {"blocked": "exchange_error",
                        "error": str(e)[:200]}
            # Trade persisted; the failure came later (e.g. client close).
            # The lifecycle sweep re-checks protection on its next pass.
            logger.exception("crypto post-fill step failed trade=%s: %s",
                             trade_oid, e)

        trade_doc["id"] = str(trade_oid)
        trade_doc.pop("_id", None)
        if protect_result is not None:
            trade_doc["protection_outcome"] = protect_result.get("outcome")
        await ws_manager.broadcast(user_id, "trade_created", trade_doc)

        try:
            from notifier import notify_trade_opened
            sent = await notify_trade_opened(user_id, trade_doc)
            if sent:
                await db.trades.update_one(
                    {"_id": trade_oid}, {"$set": {"notified_opened": True}},
                )
        except Exception:  # noqa: BLE001
            pass

        return trade_doc

    @staticmethod
    async def _reserve_exposure(db, *, user_id, account, acct_id, cfg_account_id,
                                symbol, amount, risk_usd, safety, origin,
                                max_concurrent) -> dict:
        """Reserve one slot + this entry's stop risk (quote-currency $) via
        execution_authority.reserve_exposure. Same caps as the MT5 adapter
        (execution.reserve_trade_exposure): auto slots < max_concurrent,
        total < max_concurrent + EXEC_TOTAL_POSITIONS_BUFFER, and — when the
        Safety Guardian ran its aggregate check — open risk ≤
        SAFETY_MAX_TOTAL_OPEN_RISK_PCT of equity. Fails closed on db errors
        (reserve_exposure returns a typed block)."""
        from execution_authority import RESERVATIONS, reserve_exposure
        if not callable(getattr(getattr(db, RESERVATIONS, None),
                                "find_one_and_update", None)):
            # Same semantics as reserve_exposure's ReservationStoreUnavailable
            # (non-motor test double): the legacy count gate above already
            # ran. A real motor collection always exposes this method, so
            # production database errors still FAIL CLOSED below.
            logger.error("exposure reservation store unavailable (db handle "
                         "lacks find_one_and_update) — legacy count gate only")
            return {"ok": True, "reservation": None,
                    "skipped": "reservation_store_unavailable"}
        equity = float(account.get("equity") or account.get("balance") or 0)
        ctx = (safety or {}).get("context") or {}
        max_risk = None
        if ((safety or {}).get("ok") and ctx.get("aggregate_risk") is not None
                and equity > 0):
            from safety_guardian import MAX_TOTAL_OPEN_RISK_PCT
            max_risk = equity * (MAX_TOTAL_OPEN_RISK_PCT / 100.0)
        max_total = None
        if max_concurrent > 0:
            max_total = max_concurrent + int(os.environ.get(
                "EXEC_TOTAL_POSITIONS_BUFFER", "2"))
        return await reserve_exposure(
            db, user_id=user_id, account_id=acct_id,
            cfg_account_id=cfg_account_id, symbol=symbol or "",
            lot=float(amount or 0), risk_usd=float(risk_usd or 0),
            auto=origin == "auto", max_concurrent=max_concurrent,
            max_total=max_total, max_risk_usd=max_risk, account=account)

    @staticmethod
    async def _release(db, reservation) -> None:
        if not reservation:
            return
        try:
            from execution_authority import release_reservation
            await release_reservation(db, reservation)
        except Exception as e:  # noqa: BLE001 — rebuild heals a miss
            logger.warning("exposure reservation release failed: %s", e)

    @staticmethod
    async def _enrich_and_insert(db, user_id, symbol_internal, signal, trade_doc) -> None:
        try:
            from trade_explainer import snapshot_for_trade_creation
            trade_doc["explanation_snapshot"] = await snapshot_for_trade_creation(
                db, user_id=user_id, symbol=symbol_internal,
                signal=signal, trade_doc=trade_doc,
            )
        except Exception:  # noqa: BLE001
            pass
        await db.trades.insert_one(trade_doc)

    @staticmethod
    def _build_trade_doc(*, trade_oid, user_id, account, signal, symbol_internal,
                         ccxt_symbol, action, amount, entry_px, sl_px, order_type,
                         order_resp, client_order_id, safety, crypto_risk_usd,
                         crypto_cap, cap_pct_used, per_account_cap,
                         entry_uncertain) -> dict:
        ex_status = str(order_resp.get("status") or "").lower()
        filled = float(order_resp.get("filled") or 0)
        # 'open' only on a CONFIRMED fill: market orders normally return
        # status 'closed' (an IOC-expired market with a partial fill opens
        # for the filled size). Anything else — a resting limit, an ack
        # without fill info, an unknown outcome — stays 'pending' until the
        # lifecycle sweep confirms the fill from the exchange.
        is_filled = (not entry_uncertain) and (
            ex_status == "closed"
            or (order_type == "market" and filled > 0
                and ex_status in ("canceled", "expired")))
        if is_filled and filled > 0:
            amount = filled
        fill_price = (
            order_resp.get("average")
            or order_resp.get("price")
            or entry_px
            or 0.0
        )
        trade_doc = {
            "_id": trade_oid,
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
            "status": "open" if is_filled else "pending",
            "mode": "live" if not _is_testnet(account) else "paper",
            "broker": "BINANCE_SPOT",
            "broker_kind": "binance",
            "testnet": _is_testnet(account),
            "mt5_ticket": None,
            "exchange_order_id": str(order_resp.get("id") or ""),
            "client_order_id": client_order_id,
            "exchange_order_status": ("unknown" if entry_uncertain
                                      else order_resp.get("status")),
            "entry_order_type": order_type,
            "entry_uncertain": entry_uncertain,
            "exchange_id": (account.get("exchange_id") or "binance").lower(),
            "protection": {"status": "pending"},
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": None,
            "error": None,
            "origin": signal.get("origin", "manual"),
            "source": "binance",
            "broker_identity": {
                "exchange_id": account.get("exchange_id") or "binance",
                "api_key_fingerprint": account.get("api_key_fingerprint"),
                "account_number": account.get("account_number"),
            },
            "partial_closed": False,
            "breakeven_set": False,
            "trail_active": False,
            "safety_audit": safety,
            "crypto_risk_cap": {
                "risk_usd": round(crypto_risk_usd, 4),
                "cap_usd": round(crypto_cap, 4),
                "applied_pct": cap_pct_used,
                "cap_source": "per_account" if per_account_cap is not None else "env_default",
            },
        }

        return trade_doc
