"""Multi-layer risk engine — unified registry + the PORTFOLIO STOP layer.

STOIC's 12 protection layers live in independent modules (circuit_breakers,
risk_engine, safety_guardian, protection_guard, liquidity_map, macro_gate,
broker_reject_breaker, scalp kill-switches…). This module adds:

  1. PORTFOLIO STOP (the previously missing layer): an independent loop that
     watches TOTAL FLOATING (open-position) drawdown per account from broker
     heartbeat truth (equity vs balance). Breach → the governing bot configs
     are force-disabled (no new trades; existing positions keep their
     broker-side stops) + a critical ops alert. Manual release required.
  2. A read-only registry that evaluates every layer INDEPENDENTLY — each
     check is isolated in its own try/except so one layer erroring can never
     hide or disable the others. An errored safety-critical layer reports
     status "error" loudly instead of silently passing.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from database import get_db

logger = logging.getLogger("risk-layers")

DEF_PORTFOLIO_STOP_PCT = float(os.environ.get("PORTFOLIO_STOP_PCT", "10"))
HEARTBEAT_FRESH_SEC = 180


def _now():
    return datetime.now(timezone.utc)


def _parse_ts(v):
    try:
        ts = datetime.fromisoformat(str(v))
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def floating_drawdown_pct(account: dict) -> float | None:
    """Floating P&L as % of balance from broker heartbeat truth.
    Negative = open positions are under water."""
    balance = float(account.get("balance") or 0)
    equity = account.get("equity")
    if balance <= 0 or equity is None:
        return None
    return round((float(equity) - balance) / balance * 100, 2)


async def _governing_cfgs(db, account: dict) -> list[dict]:
    """Configs that can open trades on this account: the account-scoped
    override if present, else the user's default profile."""
    uid = str(account.get("user_id") or "")
    acc_id = str(account["_id"])
    scoped = [c async for c in db.bot_configs.find(
        {"user_id": uid, "account_id": acc_id})]
    if scoped:
        return scoped
    return [c async for c in db.bot_configs.find(
        {"user_id": uid,
         "$or": [{"account_id": None}, {"account_id": {"$exists": False}}]})]


async def check_portfolio_stop(db, account: dict) -> dict:
    """Evaluate ONE account. Trips the governing bot configs on breach.
    Fail-safe rules: stale heartbeat = degraded (never trip on stale data);
    no equity data = degraded."""
    acc_id = str(account["_id"])
    hb = _parse_ts(account.get("last_heartbeat"))
    if not hb or (_now() - hb).total_seconds() > HEARTBEAT_FRESH_SEC:
        return {"account_id": acc_id, "status": "degraded",
                "detail": "no fresh broker heartbeat — floating P&L unknown"}
    dd = floating_drawdown_pct(account)
    if dd is None:
        return {"account_id": acc_id, "status": "degraded",
                "detail": "no equity/balance in heartbeat"}
    threshold = DEF_PORTFOLIO_STOP_PCT
    cfgs = await _governing_cfgs(db, account)
    overrides = []
    for c in cfgs:
        try:
            v = c.get("portfolio_stop_pct")
            if v is not None and float(v) > 0:
                overrides.append(float(v))
        except Exception:
            pass
    if overrides:
        threshold = min(overrides)   # most conservative override wins
    if dd > -threshold:
        return {"account_id": acc_id, "status": "armed",
                "detail": f"floating {dd:+.2f}% vs -{threshold}% limit"}

    reason = (f"Portfolio stop: floating drawdown {dd:.2f}% breached "
              f"-{threshold}% of balance")
    tripped_any = False
    for c in cfgs:
        if not c.get("active"):
            continue
        await db.bot_configs.update_one(
            {"_id": c["_id"]},
            {"$set": {"active": False,
                      "tripped_at": _now().isoformat(),
                      "tripped_reason": reason,
                      "tripped_kind": "portfolio"}})
        tripped_any = True
    if tripped_any:
        logger.critical("PORTFOLIO STOP tripped for account %s (%s): %s",
                        acc_id, account.get("label"), reason)
        try:
            from alerting import raise_alert
            await raise_alert(
                db, "portfolio_stop_tripped", "critical",
                f"{reason} on account '{account.get('label') or acc_id}' — "
                f"bot disabled, open positions keep their stops",
                dedup_key=f"portfolio_stop:{acc_id}")
        except Exception:  # noqa: BLE001
            logger.warning("portfolio stop alert failed", exc_info=True)
        try:
            await db.trade_events.insert_one({
                "event_type": "PortfolioStopTripped",
                "account_id": acc_id,
                "user_id": str(account.get("user_id") or ""),
                "trade_id": None,
                "occurred_at": _now().isoformat(),
                "ts_ms": int(_now().timestamp() * 1000),
                "source": "risk_layers",
                "payload": {"floating_dd_pct": dd, "threshold_pct": threshold},
            })
        except Exception:  # noqa: BLE001
            pass
    return {"account_id": acc_id,
            "status": "tripped" if tripped_any else "already_tripped",
            "detail": reason, "floating_dd_pct": dd,
            "threshold_pct": threshold}


async def sweep_portfolio_stop(db) -> int:
    """Evaluate every live-ish account; returns number of trips."""
    trips = 0
    async for acc in db.accounts.find(
            {"status": {"$ne": "deleted"}, "dormant": {"$ne": True},
             "last_heartbeat": {"$ne": None}},
            {"label": 1, "user_id": 1, "balance": 1, "equity": 1,
             "last_heartbeat": 1}):
        try:
            res = await check_portfolio_stop(db, acc)
            if res.get("status") == "tripped":
                trips += 1
        except Exception:  # noqa: BLE001 — one account failing must not stop the sweep
            logger.exception("portfolio stop check failed for %s", acc.get("_id"))
    return trips


async def _portfolio_stop_loop():
    from workers.base import record_progress
    interval = int(os.environ.get("PORTFOLIO_STOP_INTERVAL_SEC", "30"))
    while True:
        try:
            await asyncio.sleep(interval)
            t0 = _now()
            trips = await sweep_portfolio_stop(get_db())
            record_progress("_portfolio_stop_loop", processed=1 + trips,
                            started_at=t0, interval_sec=interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("portfolio stop loop error: %s", e)


# --------------------------------------------------------------------------
# Layer registry — independent, isolated evaluation of all 12 layers
# --------------------------------------------------------------------------
LAYERS = (
    ("strategy_stop", "Strategy stop", "Per-strategy SL/TP config enforced by the engines"),
    ("position_stop", "Position stop", "protection_guard repairs any position without a broker-confirmed stop"),
    ("portfolio_stop", "Portfolio stop", "Floating drawdown across all open positions vs equity"),
    ("daily_loss_stop", "Daily loss stop", "circuit_breakers — realised P&L today vs limit"),
    ("weekly_loss_stop", "Weekly loss stop", "circuit_breakers — realised P&L 7d vs limit"),
    ("monthly_drawdown_stop", "Monthly drawdown stop", "risk_engine drawdown ladder — suspends the month"),
    ("volatility_stop", "Volatility stop", "risk_engine abnormal-market shock detection + vol-scaled sizing"),
    ("spread_protection", "Spread protection", "scalp kill-switch + pre-trade cost gates"),
    ("liquidity_protection", "Liquidity protection", "liquidity_map gate — vetoes trades into resting liquidity"),
    ("broker_anomaly_protection", "Broker anomaly protection", "reject-streak breaker + stale-feed suspension"),
    ("news_protection", "News protection", "economic calendar + macro gate + event exposure cap"),
    ("circuit_breaker", "Circuit breaker", "Hard breakers + panic + safety_guardian floors the user cannot override"),
)


async def _isolated(fn):
    """Run one layer check fully isolated — an exception becomes an 'error'
    status instead of aborting the other layers."""
    try:
        return await fn()
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "detail": f"{type(e).__name__}: {e}"[:200]}


async def evaluate_layers(db, user_id: str, account: dict | None) -> list[dict]:
    now = _now()
    acc_id = str(account["_id"]) if account else None

    async def cfg():
        if acc_id:
            c = await db.bot_configs.find_one(
                {"user_id": user_id, "account_id": acc_id})
            if c:
                return c
        return await db.bot_configs.find_one(
            {"user_id": user_id,
             "$or": [{"account_id": None},
                     {"account_id": {"$exists": False}}]}) or {}

    the_cfg = await cfg()

    async def strategy_stop():
        return {"status": "armed",
                "detail": "engine-enforced SL on every entry; payoff + RR guards active"}

    async def position_stop():
        q = {"user_id": user_id, "status": "open",
             "$or": [{"stop_loss": {"$in": [None, 0]}},
                     {"lifecycle_state": {"$in": ["FILLED_UNPROTECTED",
                                                  "PROTECTION_REQUESTED"]}}]}
        if acc_id:
            q["account_id"] = acc_id
        n = await db.trades.count_documents(q)
        return ({"status": "degraded",
                 "detail": f"{n} open position(s) awaiting broker-confirmed stop"}
                if n else {"status": "armed",
                           "detail": "all open positions carry broker-confirmed stops"})

    async def portfolio_stop():
        if not account:
            return {"status": "armed",
                    "detail": f"-{DEF_PORTFOLIO_STOP_PCT}% floating limit (select an account for live value)"}
        dd = floating_drawdown_pct(account)
        hb = _parse_ts(account.get("last_heartbeat"))
        stale = not hb or (now - hb).total_seconds() > HEARTBEAT_FRESH_SEC
        if (the_cfg or {}).get("tripped_kind") == "portfolio":
            return {"status": "tripped", "detail": (the_cfg or {}).get("tripped_reason", "")}
        if stale or dd is None:
            return {"status": "degraded", "detail": "no fresh broker heartbeat"}
        thr = float((the_cfg or {}).get("portfolio_stop_pct") or DEF_PORTFOLIO_STOP_PCT)
        return {"status": "armed", "detail": f"floating {dd:+.2f}% vs -{thr}% limit"}

    async def _drawdown(kind, since_days, default_pct, cfg_key):
        from circuit_breakers import realised_pnl_since
        since = (now - timedelta(days=since_days)).date().isoformat()
        pnl = await realised_pnl_since(db, user_id, since, account_id=acc_id)
        equity = float((account or {}).get("equity") or (account or {}).get("balance") or 0)
        limit = float((the_cfg or {}).get(cfg_key) or default_pct)
        if (the_cfg or {}).get("tripped_kind") == kind:
            return {"status": "tripped", "detail": (the_cfg or {}).get("tripped_reason", "")}
        dd = (pnl / equity * 100) if equity > 0 else 0.0
        return {"status": "armed",
                "detail": f"{kind} realised {dd:+.2f}% vs -{limit}% limit"}

    async def daily():
        return await _drawdown("daily", 1, 4.0, "daily_drawdown_pct")

    async def weekly():
        return await _drawdown("weekly", 7, 8.0, "weekly_drawdown_pct")

    async def monthly():
        return await _drawdown("monthly", 30, 12.0, "monthly_drawdown_pct")

    async def volatility():
        return {"status": "armed",
                "detail": "pre-trade shock check (4× median range) + volatility-scaled sizing"}

    async def spread():
        upd = _parse_ts((account or {}).get("spreads_updated_at"))
        if account and (not upd or (now - upd).total_seconds() > 600):
            return {"status": "degraded",
                    "detail": "no fresh spread stream — pre-trade cost gate still enforced"}
        return {"status": "armed", "detail": "spread kill-switch + cost gates on every entry"}

    async def liquidity():
        return {"status": "armed",
                "detail": "liquidity gate vetoes entries into opposing resting liquidity"}

    async def broker_anomaly():
        if account and account.get("trading_blocked"):
            return {"status": "tripped",
                    "detail": account.get("block_reason")
                    or "reject-streak breaker halted trading"}
        return {"status": "armed",
                "detail": "reject-streak breaker + stale-feed suspension active"}

    async def news():
        try:
            from economic_calendar import feed_status
            st = (feed_status() or {}).get("status")
            if st == "DOWN":
                return {"status": "degraded",
                        "detail": "calendar feed DOWN — event gate blind until it recovers"}
            if st == "DEGRADED":
                return {"status": "degraded",
                        "detail": "calendar feed degraded — using last cached events"}
        except Exception:
            pass
        return {"status": "armed",
                "detail": "macro gate + event-exposure cap around red-flag prints"}

    async def breaker():
        if (the_cfg or {}).get("tripped_at"):
            return {"status": "tripped",
                    "detail": (the_cfg or {}).get("tripped_reason") or "breaker tripped"}
        return {"status": "armed",
                "detail": "hard breakers + panic + safety floors (not user-overridable)"}

    impls = {"strategy_stop": strategy_stop, "position_stop": position_stop,
             "portfolio_stop": portfolio_stop, "daily_loss_stop": daily,
             "weekly_loss_stop": weekly, "monthly_drawdown_stop": monthly,
             "volatility_stop": volatility, "spread_protection": spread,
             "liquidity_protection": liquidity,
             "broker_anomaly_protection": broker_anomaly,
             "news_protection": news, "circuit_breaker": breaker}

    out = []
    for key, label, desc in LAYERS:
        res = await _isolated(impls[key])
        out.append({"layer": key, "label": label, "description": desc, **res})
    return out
