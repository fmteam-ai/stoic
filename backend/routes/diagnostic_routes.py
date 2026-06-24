"""Admin-only auto-diagnostic — runs every veto/health check in one shot.

Returns a structured pass/fail report covering:
  a. Connectivity     — MT5 EA heartbeat, account online, broker reachability
  b. Execution layer  — last signals + which veto blocked each
  c. Trade sync       — broker vs DB drift, ghosts, stuck modifications
  d. Risk state       — anti-tilt freeze, daily PnL guard, trade-of-day cap
  e. EA / config      — EA version, bot toggle, missing API keys
  f. Broker errors    — MT5 retcodes from last 24h (plain-English explanation)

Plus POST /diagnostic/auto-fix to run safe remediations in one click.
"""
from datetime import datetime, timezone, timedelta
import os
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from database import get_db
from trade_reconciler import reconcile_user

router = APIRouter(prefix="/diagnostic", tags=["diagnostic"])

LATEST_EA = "1.26"
HEARTBEAT_FRESH_SEC = 300

# Retcode → human explanation
_RETCODE_HINTS = {
    "10004": "REQUOTE — broker repriced; usually transient",
    "10006": "REJECT — broker declined; check symbol/leverage",
    "10013": "INVALID_REQUEST — bad order params (symbol name in EA?)",
    "10014": "INVALID_VOLUME — lot below broker minimum",
    "10016": "INVALID_STOPS — SL/TP too close to entry",
    "10018": "MARKET_CLOSED — symbol not tradable now",
    "10019": "NO_MONEY — insufficient margin",
    "10027": "AUTOTRADING_DISABLED — turn ON the 'Algo Trading' button in your MT5 toolbar (the green circular icon) + tick 'Allow Algo Trading' in the EA's properties (right-click EA → Properties → Common tab)",
}


def _admin_only(user) -> None:
    if user.get("role") != "admin" and user.get("email") != "admin@trading.bot":
        raise HTTPException(status_code=403, detail="Admin only")


def _mk(label: str, status: str, detail: str = "", fix_code: Optional[str] = None,
        fix_label: Optional[str] = None) -> dict:
    return {"label": label, "status": status, "detail": detail,
            "fix_code": fix_code, "fix_label": fix_label}


def _section_status(checks: list[dict]) -> str:
    if any(c["status"] == "fail" for c in checks):
        return "fail"
    if any(c["status"] == "warn" for c in checks):
        return "warn"
    return "pass"


async def _check_connectivity(db, user_id: str) -> dict:
    checks: list[dict] = []
    accs = await db.accounts.find({
        "user_id": user_id,
        "$or": [{"mode": "live"}, {"mode": {"$exists": False}}],
    }).to_list(length=20)
    if not accs:
        checks.append(_mk("MT5 accounts linked", "fail",
                          "No live accounts connected.",
                          fix_code=None,
                          fix_label="Go to Accounts → Connect MT5"))
    else:
        checks.append(_mk("MT5 accounts linked", "pass",
                          f"{len(accs)} live account(s)"))
        now = datetime.now(timezone.utc)
        fresh = 0
        stale_labels = []
        for a in accs:
            hb = a.get("last_heartbeat")
            if not hb:
                stale_labels.append(a.get("label") or "unnamed")
                continue
            try:
                dt = datetime.fromisoformat(str(hb).replace("Z", "+00:00"))
                if (now - dt).total_seconds() <= HEARTBEAT_FRESH_SEC:
                    fresh += 1
                else:
                    stale_labels.append(a.get("label") or "unnamed")
            except Exception:
                stale_labels.append(a.get("label") or "unnamed")
        if stale_labels:
            checks.append(_mk("EA heartbeat fresh (<5min)", "fail",
                              f"Stale: {', '.join(stale_labels)}",
                              fix_label="Restart MT5 + reattach STOIC EA"))
        else:
            checks.append(_mk("EA heartbeat fresh (<5min)", "pass",
                              f"{fresh}/{len(accs)} accounts heartbeating"))
    return {"id": "connectivity", "title": "Connectivity (MT5 + Broker)",
            "checks": checks, "status": _section_status(checks)}


async def _check_execution(db, user_id: str) -> dict:
    checks: list[dict] = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    sigs = await db.signals.find({
        "user_id": user_id,
        "created_at": {"$gte": cutoff},
    }).sort("created_at", -1).limit(50).to_list(length=50)

    if not sigs:
        checks.append(_mk("Signals generated (last 6h)", "warn",
                          "No signals yet — first tick may still be pending."))
    else:
        tradeable = [s for s in sigs if s.get("action") in ("BUY", "SELL")]
        holds = [s for s in sigs if s.get("action") == "HOLD"]
        vetoed = [s for s in sigs if s.get("veto_reason")]
        checks.append(_mk("Signals generated (last 6h)", "pass",
                          f"{len(sigs)} total · {len(tradeable)} tradeable · "
                          f"{len(holds)} HOLD · {len(vetoed)} vetoed"))

        # Veto breakdown
        if vetoed:
            from collections import Counter
            reasons = Counter(s.get("veto_reason", "unknown") for s in vetoed)
            top = ", ".join(f"{k}×{v}" for k, v in reasons.most_common(5))
            checks.append(_mk("Top veto reasons", "warn", top))
        else:
            checks.append(_mk("Top veto reasons", "pass", "No signals vetoed"))

        # Did anything actually execute?
        recent_trade = await db.trades.find_one({
            "user_id": user_id,
            "origin": "auto",
            "opened_at": {"$gte": cutoff},
        })
        if tradeable and not recent_trade:
            checks.append(_mk("Tradeable signals → executed trades", "fail",
                              f"{len(tradeable)} tradeable signals but 0 executed trades. "
                              f"Check anti-tilt / max-concurrent / broker errors."))
        elif tradeable and recent_trade:
            checks.append(_mk("Tradeable signals → executed trades", "pass",
                              "Signals are firing into broker"))
    return {"id": "execution", "title": "Execution Pipeline",
            "checks": checks, "status": _section_status(checks)}


async def _check_trade_sync(db, user_id: str) -> dict:
    checks: list[dict] = []
    # Ghost trades
    ghosts = await db.trades.count_documents({
        "user_id": user_id, "status": "closed", "exit_price": None,
        "ghost_acknowledged": {"$ne": True},
    })
    if ghosts > 0:
        checks.append(_mk("Ghost trades (closed without exit_price)", "warn",
                          f"{ghosts} trade(s) — EA v1.26 history sweep should auto-fill",
                          fix_code="ack_ghost_trades",
                          fix_label="Acknowledge all ghosts"))
    else:
        checks.append(_mk("Ghost trades (closed without exit_price)", "pass",
                          "No ghosts"))

    # Stuck modifications >5min
    five_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    stuck = await db.trades.count_documents({
        "user_id": user_id, "status": "open",
        "pending_modification": {"$ne": None},
        "pending_modification.requested_at": {"$lt": five_min_ago},
    })
    if stuck > 0:
        checks.append(_mk("Stuck SL/TP modifications", "warn",
                          f"{stuck} modification(s) waiting >5min — EA may have ignored",
                          fix_code="clear_stuck_modifications",
                          fix_label="Clear stuck modifications"))
    else:
        checks.append(_mk("Stuck SL/TP modifications", "pass", "Queue is clean"))

    # Broker vs DB drift (open trades in DB vs heartbeat count)
    db_open = await db.trades.count_documents({
        "user_id": user_id, "status": {"$in": ["pending", "open"]},
    })
    accs = await db.accounts.find({"user_id": user_id}).to_list(length=20)
    broker_open = sum(int(a.get("positions_count") or 0) for a in accs)
    if abs(db_open - broker_open) > 0:
        checks.append(_mk("Broker positions ↔ DB sync", "warn",
                          f"DB={db_open} · Broker reports {broker_open}",
                          fix_code="reconcile_trades",
                          fix_label="Force reconcile"))
    else:
        checks.append(_mk("Broker positions ↔ DB sync", "pass",
                          f"In sync: {db_open}"))
    return {"id": "trade_sync", "title": "Trade Synchronization",
            "checks": checks, "status": _section_status(checks)}


async def _check_risk_state(db, user_id: str) -> dict:
    checks: list[dict] = []
    now = datetime.now(timezone.utc)
    cfgs = await db.bot_configs.find({"user_id": user_id}).to_list(length=20)

    # Anti-tilt freeze
    frozen_scopes = []
    for tc in cfgs:
        if not tc.get("anti_tilt_enabled", True):
            continue
        atn = int(tc.get("anti_tilt_consecutive_losses", 3) or 0)
        ath = int(tc.get("anti_tilt_freeze_hours", 4) or 0)
        if atn <= 0 or ath <= 0:
            continue
        q = {"user_id": user_id, "status": "closed"}
        if tc.get("account_id"):
            q["account_id"] = tc["account_id"]
        recent = await db.trades.find(q).sort("closed_at", -1).limit(atn).to_list(length=atn)
        if len(recent) == atn and all(float(r.get("pnl") or 0) <= 0 for r in recent):
            last_close = recent[0].get("closed_at")
            try:
                lc = datetime.fromisoformat(str(last_close).replace("Z", "+00:00"))
                unfreeze = lc + timedelta(hours=ath)
                rem = (unfreeze - now).total_seconds()
                if rem > 0:
                    mins = int(rem // 60)
                    hrs, mm = divmod(mins, 60)
                    eta = f"{hrs}h {mm}m" if hrs else f"{mm}m"
                    frozen_scopes.append(f"{tc.get('account_id') or 'default'} (resumes in {eta})")
            except Exception:
                pass
    if frozen_scopes:
        checks.append(_mk("Anti-tilt freeze", "fail",
                          "Bot frozen on: " + ", ".join(frozen_scopes),
                          fix_code="release_anti_tilt",
                          fix_label="Release anti-tilt freeze"))
    else:
        checks.append(_mk("Anti-tilt freeze", "pass", "No active freeze"))

    # Safety Guardian: confirm hard-floors are active + report recent blocks.
    try:
        from safety_guardian import get_guardian_config
        gc = get_guardian_config()
        checks.append(_mk(
            "Safety Guardian active",
            "pass",
            f"Per-trade risk ≤ {gc['max_risk_pct_per_trade']}% · "
            f"Daily loss ≤ {gc['max_daily_loss_pct']}% · "
            f"Aggregate risk ≤ {gc['max_total_open_risk_pct']}% · "
            f"Equity floor {gc['min_equity_vs_balance_pct']}% · "
            f"Free margin floor {gc['min_free_margin_pct']}%",
        ))
        # Count safety blocks persisted in the last 24h. The engine inserts
        # a `safety_blocks` row each time the guardian refuses a trade, so
        # this gives operators an honest view of risk-envelope breaches.
        day_ago = (now - timedelta(hours=24)).isoformat()
        recent_blocks = await db.safety_blocks.count_documents({
            "user_id": user_id, "blocked_at": {"$gte": day_ago},
        })
        if recent_blocks > 0:
            top_reasons = await db.safety_blocks.aggregate([
                {"$match": {"user_id": user_id, "blocked_at": {"$gte": day_ago}}},
                {"$group": {"_id": "$blocked_by", "n": {"$sum": 1}}},
                {"$sort": {"n": -1}}, {"$limit": 5},
            ]).to_list(length=5)
            breakdown = ", ".join(f"{r['_id']}×{r['n']}" for r in top_reasons)
            checks.append(_mk(
                "Safety Guardian recent blocks (24h)", "warn",
                f"{recent_blocks} trade(s) refused — {breakdown}. "
                "Bot is attempting trades outside risk envelope.",
            ))
        else:
            checks.append(_mk("Safety Guardian recent blocks (24h)", "pass",
                              "0 blocks — bot operating within risk envelope"))
    except Exception as e:
        checks.append(_mk("Safety Guardian active", "fail",
                          f"Module not loadable: {e}"))

    # Daily PnL — sum today's closed trade pnl
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    today_trades = await db.trades.find({
        "user_id": user_id, "status": "closed",
        "closed_at": {"$gte": day_start},
    }).to_list(length=200)
    daily_pnl = sum(float(t.get("pnl") or 0) for t in today_trades)
    if daily_pnl < -500:
        checks.append(_mk("Daily PnL", "fail",
                          f"${daily_pnl:.2f} — deep red. Consider PANIC."))
    elif daily_pnl < 0:
        checks.append(_mk("Daily PnL", "warn", f"${daily_pnl:.2f}"))
    else:
        checks.append(_mk("Daily PnL", "pass", f"${daily_pnl:.2f}"))

    # Per-config cap audit: detect over-cap state on any scope (the bug the
    # user reported — 12 trades open with cap=5).
    over_cap_scopes = []
    for c in cfgs:
        cap = int(c.get("max_concurrent_trades", 0) or 0)
        if cap <= 0:
            continue
        scope_q = {"user_id": user_id, "status": {"$in": ["pending", "open"]}}
        if c.get("account_id"):
            scope_q["account_id"] = c["account_id"]
        inflight = await db.trades.count_documents(scope_q)
        if inflight > cap:
            over_cap_scopes.append(f"{c.get('account_id') or 'default'}: {inflight}/{cap}")
    if over_cap_scopes:
        checks.append(_mk("Max concurrent trades", "fail",
                          "OVER CAP — " + ", ".join(over_cap_scopes),
                          fix_code="close_excess_trades",
                          fix_label="Close oldest excess down to cap"))
    else:
        # Show the highest-utilisation scope as a positive signal
        max_util = "0/0"
        for c in cfgs:
            cap = int(c.get("max_concurrent_trades", 0) or 0)
            if cap <= 0:
                continue
            scope_q = {"user_id": user_id, "status": {"$in": ["pending", "open"]}}
            if c.get("account_id"):
                scope_q["account_id"] = c["account_id"]
            inflight = await db.trades.count_documents(scope_q)
            if cap > 0:
                max_util = f"{inflight}/{cap}"
        checks.append(_mk("Max concurrent trades", "pass", f"Within cap · {max_util}"))
    return {"id": "risk_state", "title": "Risk State",
            "checks": checks, "status": _section_status(checks)}


async def _check_ea_config(db, user_id: str) -> dict:
    checks: list[dict] = []
    accs = await db.accounts.find({"user_id": user_id}).to_list(length=20)
    outdated = [a.get("label") or "unnamed" for a in accs
                if (a.get("ea_version") or "") < LATEST_EA and a.get("ea_version")]
    if outdated:
        checks.append(_mk(f"EA version (latest {LATEST_EA})", "warn",
                          "Outdated on: " + ", ".join(outdated),
                          fix_label="Recompile EA in MetaEditor (F7)"))
    else:
        checks.append(_mk(f"EA version (latest {LATEST_EA})", "pass",
                          f"All terminals on v{LATEST_EA} or newer"))

    cfgs = await db.bot_configs.find({"user_id": user_id}).to_list(length=20)
    any_active = any(c.get("active") for c in cfgs)
    if not any_active:
        checks.append(_mk("Bot ON toggle", "fail",
                          "All bot configs are paused.",
                          fix_label="Toggle Bot ON in Bot Config"))
    else:
        active_count = sum(1 for c in cfgs if c.get("active"))
        checks.append(_mk("Bot ON toggle", "pass",
                          f"{active_count}/{len(cfgs)} config(s) active"))

    # API keys
    has_claude = bool(os.environ.get("EMERGENT_LLM_KEY"))
    if has_claude:
        checks.append(_mk("Claude (Emergent LLM key)", "pass", "Configured"))
    else:
        checks.append(_mk("Claude (Emergent LLM key)", "fail",
                          "Missing EMERGENT_LLM_KEY — AI signals disabled.",
                          fix_label="Add EMERGENT_LLM_KEY in backend/.env"))

    # Per-user Telegram (notifications collection)
    tg = await db.notifications.find_one({"user_id": user_id, "kind": "telegram"})
    if tg and tg.get("chat_id"):
        checks.append(_mk("Telegram alerts", "pass", "Configured for this user"))
    else:
        checks.append(_mk("Telegram alerts", "warn",
                          "Not configured — alerts will not be sent.",
                          fix_label="Set up in Settings → Notifications"))
    return {"id": "ea_config", "title": "EA / Config Health",
            "checks": checks, "status": _section_status(checks)}


async def _check_broker_errors(db, user_id: str) -> dict:
    checks: list[dict] = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    fails = await db.trades.find({
        "user_id": user_id, "status": "failed",
        "opened_at": {"$gte": cutoff},
    }).sort("opened_at", -1).limit(20).to_list(length=20)
    if not fails:
        checks.append(_mk("Broker-rejected trades (24h)", "pass", "None"))
    else:
        from collections import Counter
        codes = Counter()
        for f in fails:
            err = str(f.get("error") or "")
            for code in _RETCODE_HINTS:
                if code in err:
                    codes[code] += 1
                    break
        breakdown = ", ".join(f"{c}×{n} ({_RETCODE_HINTS[c]})"
                              for c, n in codes.most_common(5))
        status = "fail" if len(fails) >= 5 else "warn"
        checks.append(_mk("Broker-rejected trades (24h)", status,
                          f"{len(fails)} rejection(s). " + (breakdown or "see logs")))
    return {"id": "broker_errors", "title": "Broker Errors",
            "checks": checks, "status": _section_status(checks)}


@router.get("/run")
async def run_diagnostic(user=Depends(get_current_user)):
    """Full system diagnostic — admin only."""
    _admin_only(user)
    db = get_db()
    uid = user["id"]
    sections = [
        await _check_connectivity(db, uid),
        await _check_execution(db, uid),
        await _check_trade_sync(db, uid),
        await _check_risk_state(db, uid),
        await _check_ea_config(db, uid),
        await _check_broker_errors(db, uid),
    ]
    overall_fail = any(s["status"] == "fail" for s in sections)
    overall_warn = any(s["status"] == "warn" for s in sections)
    overall = "fail" if overall_fail else ("warn" if overall_warn else "pass")
    # Auto-fixable codes present in this report
    auto_fixable = sorted({
        c["fix_code"] for s in sections for c in s["checks"]
        if c.get("fix_code")
    })
    return {
        "ok": overall == "pass",
        "status": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "sections": sections,
        "auto_fixable_codes": auto_fixable,
    }


# ------------------- AUTO-FIX -----------------------------------------

async def _fix_clear_stuck_modifications(db, uid: str) -> dict:
    res = await db.trades.update_many(
        {"user_id": uid, "status": "open", "pending_modification": {"$ne": None}},
        {"$set": {"pending_modification": None,
                  "pending_modification_expired": True}},
    )
    return {"cleared": res.modified_count}


async def _fix_ack_ghost_trades(db, uid: str) -> dict:
    res = await db.trades.update_many(
        {"user_id": uid, "status": "closed", "exit_price": None,
         "ghost_acknowledged": {"$ne": True}},
        {"$set": {"ghost_acknowledged": True,
                  "ghost_acknowledged_at": datetime.now(timezone.utc).isoformat()}},
    )
    return {"acknowledged": res.modified_count}


async def _fix_reconcile_trades(db, uid: str) -> dict:
    res = await reconcile_user(uid, force=True)
    return res or {"reconciled": True}


async def _fix_release_anti_tilt(db, uid: str) -> dict:
    """Bypass anti-tilt freeze by ticking the most recent closed loss to PnL=0.01.

    This is admin-explicit override. The flag stays enabled (config unchanged)
    but the freeze condition (all N recent trades lost) breaks.
    """
    last = await db.trades.find_one(
        {"user_id": uid, "status": "closed", "pnl": {"$lt": 0}},
        sort=[("closed_at", -1)],
    )
    if not last:
        return {"released": False, "reason": "no recent loss to bypass"}
    await db.trades.update_one(
        {"_id": last["_id"]},
        {"$set": {"anti_tilt_admin_release": True,
                  "anti_tilt_released_at": datetime.now(timezone.utc).isoformat(),
                  "pnl_original": last.get("pnl"),
                  "pnl": 0.01}},
    )
    return {"released": True, "trade_id": str(last["_id"])}


async def _fix_close_excess_trades(db, uid: str) -> dict:
    """Close the oldest open trades on any account where the live position count
    exceeds the user's max_concurrent_trades cap. Closes the EXCESS only,
    leaving the cap many newest positions open.

    Marks the closures with close_reason='excess_over_cap' so the EA will close
    them on its next poll and accounting stays clean.
    """
    closed_total = 0
    closed_by_acct: dict = {}
    cfgs = await db.bot_configs.find({"user_id": uid}).to_list(length=20)
    for cfg in cfgs:
        cap = int(cfg.get("max_concurrent_trades", 0) or 0)
        if cap <= 0:
            continue
        scope_q = {"user_id": uid, "status": {"$in": ["pending", "open"]}}
        if cfg.get("account_id"):
            scope_q["account_id"] = cfg["account_id"]
        inflight = await db.trades.count_documents(scope_q)
        if inflight <= cap:
            continue
        # Close the OLDEST trades down to the cap
        to_close = inflight - cap
        oldest = await db.trades.find(scope_q).sort("opened_at", 1).limit(to_close).to_list(length=to_close)
        for t in oldest:
            await db.trades.update_one(
                {"_id": t["_id"]},
                {"$set": {"close_requested": True,
                          "close_reason": "excess_over_cap"}},
            )
        scope_label = cfg.get("account_id") or "default"
        closed_by_acct[scope_label] = to_close
        closed_total += to_close
    return {"closed": closed_total, "by_account": closed_by_acct}


_FIX_REGISTRY = {
    "clear_stuck_modifications": _fix_clear_stuck_modifications,
    "ack_ghost_trades": _fix_ack_ghost_trades,
    "reconcile_trades": _fix_reconcile_trades,
    "release_anti_tilt": _fix_release_anti_tilt,
    "close_excess_trades": _fix_close_excess_trades,
}


@router.post("/auto-fix")
async def auto_fix(payload: dict, user=Depends(get_current_user)):
    """Apply one or more safe remediations. Admin only.

    Body: { "codes": ["clear_stuck_modifications", "ack_ghost_trades", ...] }
    """
    _admin_only(user)
    db = get_db()
    uid = user["id"]
    codes = payload.get("codes") or []
    if not isinstance(codes, list) or not codes:
        raise HTTPException(status_code=400, detail="codes (list) required")
    results: dict = {}
    for c in codes:
        fn = _FIX_REGISTRY.get(c)
        if not fn:
            results[c] = {"error": "unknown fix code"}
            continue
        try:
            results[c] = await fn(db, uid)
        except Exception as e:
            results[c] = {"error": str(e)}
    return {"applied_at": datetime.now(timezone.utc).isoformat(),
            "results": results}
