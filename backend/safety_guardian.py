"""Safety Guardian — server-side hard floors for LIVE accounts.

Defence-in-depth layer that runs RIGHT BEFORE a `pending` trade is inserted
into MongoDB. Validates the proposed trade against absolute caps derived
from live account equity and recent trading activity. If any cap is breached,
the trade is refused — regardless of user config (the user CAN'T disable
these floors via UI, because they exist to protect users from bot failure).

Every approved trade gets stamped with a `safety_audit` dict so the user can
inspect exactly what the guards saw at trade time.

Constants are env-overridable (so power-users can loosen them) but ship
with conservative defaults sized to survive an 80%-confidence model crash.
"""
from datetime import datetime, timezone
import logging
import os

from pip_utils import price_to_pips, pip_value_usd_per_lot
from macro_gate import evaluate as macro_gate_evaluate

logger = logging.getLogger("safety-guardian")


def _f(env_key: str, default: float) -> float:
    try:
        return float(os.environ.get(env_key, default))
    except Exception:
        return default


# Per-trade caps (% of account equity)
MAX_RISK_PCT_PER_TRADE   = _f("SAFETY_MAX_RISK_PCT_PER_TRADE", 3.0)   # SL-distance × lot × pip_usd
MAX_LOT_PCT_OF_EQUITY    = _f("SAFETY_MAX_LOT_PCT_OF_EQUITY", 5.0)    # lot * notional / equity

# Aggregate caps (account-wide, all open trades)
MAX_TOTAL_OPEN_RISK_PCT  = _f("SAFETY_MAX_TOTAL_OPEN_RISK_PCT", 9.0)  # sum(risk) of open trades
MAX_DAILY_LOSS_PCT       = _f("SAFETY_MAX_DAILY_LOSS_PCT", 6.0)        # today's realized + unrealized

# Account-health gates
MIN_FREE_MARGIN_PCT      = _f("SAFETY_MIN_FREE_MARGIN_PCT", 20.0)     # free_margin/equity
MIN_EQUITY_VS_BALANCE_PCT = _f("SAFETY_MIN_EQUITY_VS_BALANCE_PCT", 70.0)  # equity/balance


def _ok(name: str, value: float | str | None = None) -> dict:
    return {"name": name, "ok": True, "value": value}


def sl_tp_side_violation(signal: dict) -> str | None:
    """H6/H7 — return a reason when SL/TP sit on the wrong side of entry, else None.
    BUY: SL < entry < TP.  SELL: TP < entry < SL.  Missing/zero TP is allowed
    (SL-only trades); a missing SL is handled by the risk_inputs check."""
    action = str(signal.get("action") or "").upper()
    try:
        entry = float(signal.get("entry_price") or 0)
        sl = float(signal.get("stop_loss") or 0)
        tp = float(signal.get("take_profit") or 0)
    except (TypeError, ValueError):
        return "entry/SL/TP not numeric"
    if action not in ("BUY", "SELL") or entry <= 0:
        return None
    if action == "BUY":
        if sl > 0 and sl >= entry:
            return f"BUY stop-loss {sl} is not below entry {entry}"
        if tp > 0 and tp <= entry:
            return f"BUY take-profit {tp} is not above entry {entry}"
    else:
        if sl > 0 and sl <= entry:
            return f"SELL stop-loss {sl} is not above entry {entry}"
        if tp > 0 and tp >= entry:
            return f"SELL take-profit {tp} is not below entry {entry}"
    return None


def _fail(name: str, reason: str, value: float | str | None = None) -> dict:
    return {"name": name, "ok": False, "reason": reason, "value": value}


async def audit_pre_trade(*, db, account: dict, signal: dict,
                          user_id: str, cfg_account_id: str | None) -> dict:
    """Run all live-account safety checks. Return:
      {
        "ok": bool,                  # True = trade may proceed
        "blocked_by": str | None,    # name of first failed check
        "audit": [ {name, ok, ...}, ... ],
        "evaluated_at": iso,
        "context": { equity, balance, free_margin, ... },
      }

    For paper accounts, all checks return ok=True with a single audit entry
    noting paper-mode bypass (we still want a clear audit trail).
    """
    audit: list[dict] = []
    mode = (account.get("mode") or "live").lower()
    is_live = mode != "paper"
    # main92 P4 — scope caps to THIS account when the bot config carries no
    # account id (otherwise every account incl. paper is summed against it).
    cfg_account_id = cfg_account_id or str(account.get("id") or account.get("_id") or "") or None

    equity = float(account.get("equity") or account.get("balance") or 0)
    balance = float(account.get("balance") or equity or 0)
    free_margin = float(account.get("free_margin") or equity or 0)

    context = {
        "mode": mode, "equity": equity, "balance": balance,
        "free_margin": free_margin,
        "symbol": signal.get("symbol"), "action": signal.get("action"),
        "lot_size": float(signal.get("lot_size") or 0),
    }

    # 0. H6/H7 (roadmap step 4): protective levels must sit on the correct side of
    # entry — a BUY with SL above entry (or TP below) would be rejected or, worse,
    # filled and stopped out instantly at the broker. Applies to EVERY mode.
    side = sl_tp_side_violation(signal)
    if side:
        audit.append(_fail("sl_tp_side", side, f"entry={signal.get('entry_price')} "
                           f"sl={signal.get('stop_loss')} tp={signal.get('take_profit')}"))
        return {"ok": False, "blocked_by": "sl_tp_side", "audit": audit,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "context": context}
    audit.append(_ok("sl_tp_side"))

    if not is_live:
        audit.append(_ok("paper_mode_bypass", value="paper account — no live caps applied"))
        return {"ok": True, "blocked_by": None, "audit": audit,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "context": context}


    # 1. Equity must be positive & known
    if equity <= 0:
        audit.append(_fail("equity_known", "Account equity is 0 or unknown — refuse trade", equity))
        return {"ok": False, "blocked_by": "equity_known", "audit": audit,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "context": context}
    audit.append(_ok("equity_known", value=equity))

    # 2. Equity-vs-balance floor (drawdown halt)
    if balance > 0:
        ratio_pct = (equity / balance) * 100.0
        if ratio_pct < MIN_EQUITY_VS_BALANCE_PCT:
            audit.append(_fail("equity_vs_balance_floor",
                               f"Equity {ratio_pct:.1f}% of balance < floor {MIN_EQUITY_VS_BALANCE_PCT:.0f}%",
                               ratio_pct))
            return {"ok": False, "blocked_by": "equity_vs_balance_floor", "audit": audit,
                    "evaluated_at": datetime.now(timezone.utc).isoformat(),
                    "context": context}
        audit.append(_ok("equity_vs_balance_floor", value=f"{ratio_pct:.1f}%"))

    # 3. Free margin must be > floor (broker won't reject for margin)
    if equity > 0:
        free_pct = (free_margin / equity) * 100.0
        if free_pct < MIN_FREE_MARGIN_PCT:
            audit.append(_fail("free_margin_floor",
                               f"Free margin {free_pct:.1f}% of equity < floor {MIN_FREE_MARGIN_PCT:.0f}%",
                               free_pct))
            return {"ok": False, "blocked_by": "free_margin_floor", "audit": audit,
                    "evaluated_at": datetime.now(timezone.utc).isoformat(),
                    "context": context}
        audit.append(_ok("free_margin_floor", value=f"{free_pct:.1f}%"))

    # 4. Per-trade risk in $ (SL-distance × lot × pip_value)
    lot = float(signal.get("lot_size") or 0)
    entry = float(signal.get("entry_price") or 0)
    sl = float(signal.get("stop_loss") or 0)
    sym = signal.get("symbol") or ""
    risk_usd = 0.0
    if not (lot > 0 and entry > 0 and sl > 0):
        audit.append(_fail("risk_inputs_present",
                           "Cannot compute risk — missing lot/entry/SL",
                           f"lot={lot} entry={entry} sl={sl}"))
        return {"ok": False, "blocked_by": "risk_inputs_present", "audit": audit,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "context": context}
    audit.append(_ok("risk_inputs_present",
                     value=f"lot={lot} entry={entry} sl={sl}"))
    sl_pips = price_to_pips(sym, abs(entry - sl))
    # H8 — price-aware pip value (USD-based / cross pairs need the rate)
    pip_usd = pip_value_usd_per_lot(sym, account.get("account_type"), price=entry)
    # FAIL CLOSED (audit E8): an unknown pip value would silently understate
    # risk — refuse the trade instead of guessing.
    if not pip_usd or pip_usd <= 0 or not sl_pips or sl_pips <= 0:
        audit.append(_fail("instrument_spec_known",
                           "Pip value / stop distance could not be resolved "
                           "for this instrument — refusing trade",
                           f"pip_usd={pip_usd} sl_pips={sl_pips}"))
        return {"ok": False, "blocked_by": "instrument_spec_known",
                "audit": audit,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "context": context}
    risk_usd = sl_pips * pip_usd * lot
    max_risk_usd = equity * (MAX_RISK_PCT_PER_TRADE / 100.0)
    if risk_usd > max_risk_usd:
        audit.append(_fail("per_trade_risk_cap",
                           f"Risk ${risk_usd:.2f} > cap ${max_risk_usd:.2f} ({MAX_RISK_PCT_PER_TRADE}% of equity)",
                           risk_usd))
        return {"ok": False, "blocked_by": "per_trade_risk_cap", "audit": audit,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "context": {**context, "risk_usd": risk_usd}}
    audit.append(_ok("per_trade_risk_cap",
                     value=f"${risk_usd:.2f} <= ${max_risk_usd:.2f}"))

    # 5. Lot vs equity ratio sanity (catches Kelly explosion)
    # 1 lot XAU ≈ 100 oz ≈ $400k notional at current prices. Cap lot s.t.
    # notional ≤ MAX_LOT_PCT_OF_EQUITY * equity * leverage_assumption (500x).
    # Simpler heuristic: lot * SL$ ≤ MAX_LOT_PCT_OF_EQUITY% of equity.
    lot_value_proxy = lot * pip_usd * 100 if lot > 0 else 0  # 100 pips ~ "typical move"
    max_lot_value = equity * (MAX_LOT_PCT_OF_EQUITY / 100.0) * 5  # allow up to 5x equity exposure
    if lot_value_proxy > max_lot_value:
        audit.append(_fail("lot_vs_equity_sanity",
                           f"Lot exposure ${lot_value_proxy:.0f} > sanity cap ${max_lot_value:.0f}",
                           lot))
        return {"ok": False, "blocked_by": "lot_vs_equity_sanity", "audit": audit,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "context": {**context, "lot_value_proxy": lot_value_proxy}}
    audit.append(_ok("lot_vs_equity_sanity",
                     value=f"${lot_value_proxy:.0f} <= ${max_lot_value:.0f}"))

    # 6. Daily loss cap (realized today) — DB-side aggregation (audit E7):
    # never silently truncated by a fixed to_list cap.
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    realized_today = 0.0
    # Fix plan R8 — equity-level guard: scoped to the account when known,
    # otherwise the whole user (never skipped), and counts EVERY origin —
    # a losing manual day must also stop the bot from adding risk.
    dl_match = {"user_id": user_id, "status": "closed",
                "closed_at": {"$gte": day_start}}
    if cfg_account_id:
        dl_match["account_id"] = cfg_account_id
    async for g in db.trades.aggregate([
            {"$match": dl_match},
            {"$group": {"_id": None, "pnl": {"$sum": "$pnl"}}}]):
        realized_today = float(g.get("pnl") or 0)
    max_daily_loss = -(balance * (MAX_DAILY_LOSS_PCT / 100.0))
    if realized_today < max_daily_loss:
        audit.append(_fail("daily_loss_cap",
                           f"Today's realized ${realized_today:.2f} < cap ${max_daily_loss:.2f} ({MAX_DAILY_LOSS_PCT}%)",
                           realized_today))
        return {"ok": False, "blocked_by": "daily_loss_cap", "audit": audit,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "context": {**context, "realized_today": realized_today}}
    audit.append(_ok("daily_loss_cap",
                     value=f"${realized_today:.2f} (cap ${max_daily_loss:.2f})"))

    # 7. Total open risk (sum of remaining SL risk on all open trades + this
    # trade) — uncapped cursor scan (audit E7): no silent truncation.
    # Fix plan R8 — pending (dispatched, not yet filled) orders are exposure
    # too, and a trade WITHOUT a stop is charged the new trade's stop distance
    # (floor 100 pips) instead of counting as zero risk.
    open_q = {"user_id": user_id, "status": {"$in": ["open", "pending"]}}
    if cfg_account_id:
        open_q["account_id"] = cfg_account_id
    open_risk_usd = 0.0
    open_count = 0
    stopless_count = 0
    new_sl_pips = price_to_pips(signal.get("symbol") or "", abs(entry - sl)) if entry and sl else 0.0
    # main92 P5 — the stop-less fallback is a USD amount for the NEW trade
    # (per lot), re-expressed in the other trade's own pips below.
    new_sl_usd_per_lot = max(new_sl_pips, 100.0) * pip_usd
    async for t in db.trades.find(
            open_q, {"lot_size": 1, "entry_price": 1, "stop_loss": 1,
                     "symbol": 1, "sl_pips": 1}):
        open_count += 1
        try:
            t_lot = float(t.get("lot_size") or 0)
            t_entry = float(t.get("entry_price") or 0)
            t_sl = float(t.get("stop_loss") or 0)
            t_sym = t.get("symbol") or ""
            t_pip_usd = pip_value_usd_per_lot(t_sym, account.get("account_type"),
                                              price=t_entry or None)
            if t_lot > 0 and t_entry > 0 and t_sl > 0:
                t_pips = price_to_pips(t_sym, abs(t_entry - t_sl))
                open_risk_usd += t_pips * t_pip_usd * t_lot
            elif t_lot > 0:
                stopless_count += 1
                own_pips = float(t.get("sl_pips") or 0)
                if own_pips > 0 and t_pip_usd > 0:
                    open_risk_usd += max(own_pips * t_pip_usd, new_sl_usd_per_lot) * t_lot
                else:
                    open_risk_usd += new_sl_usd_per_lot * t_lot
            else:
                continue
        except Exception:
            continue
    aggregate_risk = open_risk_usd + risk_usd
    max_total_risk = equity * (MAX_TOTAL_OPEN_RISK_PCT / 100.0)
    if aggregate_risk > max_total_risk:
        audit.append(_fail("total_open_risk_cap",
                           f"Aggregate risk ${aggregate_risk:.2f} > cap ${max_total_risk:.2f} "
                           f"({MAX_TOTAL_OPEN_RISK_PCT}% of equity, {open_count} open/pending"
                           f"{f', {stopless_count} without stop' if stopless_count else ''} + new)",
                           aggregate_risk))
        return {"ok": False, "blocked_by": "total_open_risk_cap", "audit": audit,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "context": {**context, "aggregate_risk": aggregate_risk,
                            "open_count": open_count}}
    audit.append(_ok("total_open_risk_cap",
                     value=f"${aggregate_risk:.2f} <= ${max_total_risk:.2f}"))

    # 8. Macro-regime gate (XAUUSD only — symbol-aware, returns ok=True on
    # non-gold). Deterministic block when 10Y yields surge or USD rallies
    # against gold longs (or the inverse for shorts). Fail-open if no FRED
    # data cached yet.
    macro = await macro_gate_evaluate(signal.get("symbol") or "",
                                      signal.get("action") or "",
                                      db=db)
    if not macro["ok"]:
        audit.append(_fail("macro_regime_gate",
                           macro.get("reason") or "Macro regime adverse",
                           macro.get("regime")))
        audit.extend(macro.get("audit") or [])
        return {"ok": False, "blocked_by": "macro_regime_gate", "audit": audit,
                "evaluated_at": datetime.now(timezone.utc).isoformat(),
                "context": {**context, "macro_regime": macro.get("regime"),
                            "macro_blocked_by": macro.get("blocked_by")}}
    audit.append(_ok("macro_regime_gate", value=macro.get("regime")))

    return {"ok": True, "blocked_by": None, "audit": audit,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "context": {**context, "risk_usd": risk_usd,
                        "aggregate_risk": aggregate_risk,
                        "open_count": open_count}}


def get_guardian_config() -> dict:
    """Return current guardian thresholds — used by the diagnostic UI to
    surface what the floors actually are."""
    return {
        "max_risk_pct_per_trade": MAX_RISK_PCT_PER_TRADE,
        "max_lot_pct_of_equity": MAX_LOT_PCT_OF_EQUITY,
        "max_total_open_risk_pct": MAX_TOTAL_OPEN_RISK_PCT,
        "max_daily_loss_pct": MAX_DAILY_LOSS_PCT,
        "min_free_margin_pct": MIN_FREE_MARGIN_PCT,
        "min_equity_vs_balance_pct": MIN_EQUITY_VS_BALANCE_PCT,
    }
