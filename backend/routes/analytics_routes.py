"""Analytics routes — performance attribution endpoints."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_user
from analytics import compute_attribution, compute_sessions
from account_analytics import per_account_stats
from auto_tune import get_all_thresholds, get_auto_threshold, invalidate_cache
from learned_meta import retrain as learned_retrain, get_artifact as learned_artifact
from database import get_db

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/research")
async def research(days: int = 90, user=Depends(get_current_user)):
    """Iter-156 · research-grade analytics: confidence calibration, Wilson
    confidence intervals, walk-forward stability, regime attribution,
    strategy-decay detection and execution cost attribution."""
    import math
    from datetime import timedelta
    from bson import ObjectId
    db = get_db()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=min(max(int(days), 7), 365))).isoformat()

    trades = []
    async for t in db.trades.find(
            {"user_id": user["id"], "status": "closed", "origin": "auto",
             "closed_at": {"$gte": since}, "pnl": {"$ne": None}},
            {"pnl": 1, "closed_at": 1, "symbol": 1, "base_symbol": 1,
             "signal_id": 1}).sort("closed_at", 1).limit(3000):
        trades.append(t)

    sig_map = {}
    oids = []
    for t in trades:
        try:
            if t.get("signal_id"):
                oids.append(ObjectId(str(t["signal_id"])))
        except Exception:
            pass
    if oids:
        async for s in db.signals.find({"_id": {"$in": oids}},
                                       {"confidence": 1, "regime": 1}):
            sig_map[str(s["_id"])] = s

    def wilson(w, n, z=1.96):
        if not n:
            return None, None
        p = w / n
        den = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / den
        half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
        return (round(max(0.0, centre - half) * 100, 1),
                round(min(1.0, centre + half) * 100, 1))

    # 1 · confidence calibration — predicted bucket vs realised win rate
    cal_buckets = [(0, 49), (50, 59), (60, 69), (70, 79), (80, 89), (90, 100)]
    cal = [{"bucket": f"{lo}-{hi}", "mid": (lo + hi) / 2, "n": 0, "wins": 0}
           for lo, hi in cal_buckets]
    for t in trades:
        s = sig_map.get(str(t.get("signal_id")))
        if not s or s.get("confidence") is None:
            continue
        c = float(s["confidence"])
        idx = 0 if c < 50 else min(5, int((c - 40) // 10))
        cal[idx]["n"] += 1
        if float(t["pnl"]) > 0:
            cal[idx]["wins"] += 1
    for row in cal:
        row["win_rate"] = round(row["wins"] / row["n"] * 100, 1) if row["n"] else None
        lo, hi = wilson(row["wins"], row["n"])
        row["ci_low"], row["ci_high"] = lo, hi
        row["gap"] = (round(row["win_rate"] - row["mid"], 1)
                      if row["win_rate"] is not None else None)

    # 2 · per-symbol win-rate CIs
    sym_rows = {}
    for t in trades:
        sym = t.get("base_symbol") or t.get("symbol")
        r = sym_rows.setdefault(sym, {"symbol": sym, "n": 0, "wins": 0,
                                      "total_pnl": 0.0})
        r["n"] += 1
        r["total_pnl"] += float(t["pnl"])
        if float(t["pnl"]) > 0:
            r["wins"] += 1
    symbols = []
    for r in sorted(sym_rows.values(), key=lambda x: -x["n"]):
        r["win_rate"] = round(r["wins"] / r["n"] * 100, 1)
        r["ci_low"], r["ci_high"] = wilson(r["wins"], r["n"])
        r["total_pnl"] = round(r["total_pnl"], 2)
        symbols.append(r)

    # 3 · walk-forward stability — weekly out-of-sample buckets
    weeks = {}
    for t in trades:
        try:
            d = datetime.fromisoformat(str(t["closed_at"]))
        except Exception:
            continue
        iso = d.isocalendar()
        key = f"{iso[0]}-W{iso[1]:02d}"
        w = weeks.setdefault(key, {"week": key, "n": 0, "wins": 0,
                                   "total_pnl": 0.0})
        w["n"] += 1
        w["total_pnl"] += float(t["pnl"])
        if float(t["pnl"]) > 0:
            w["wins"] += 1
    walk = []
    cum = 0.0
    for k in sorted(weeks):
        w = weeks[k]
        w["win_rate"] = round(w["wins"] / w["n"] * 100, 1)
        w["total_pnl"] = round(w["total_pnl"], 2)
        w["avg_pnl"] = round(w["total_pnl"] / w["n"], 2)
        cum += w["total_pnl"]
        w["cum_pnl"] = round(cum, 2)
        walk.append(w)
    wf_win_rates = [w["win_rate"] for w in walk if w["n"] >= 3]
    stability = (round(100 - min(100, (max(wf_win_rates) - min(wf_win_rates))), 1)
                 if len(wf_win_rates) >= 2 else None)

    # 4 · regime attribution
    reg_rows = {}
    for t in trades:
        s = sig_map.get(str(t.get("signal_id")))
        regime = (s or {}).get("regime") or "UNKNOWN"
        if isinstance(regime, dict):
            regime = regime.get("regime") or regime.get("name") or "UNKNOWN"
        regime = str(regime)
        r = reg_rows.setdefault(regime, {"regime": regime, "n": 0, "wins": 0,
                                         "total_pnl": 0.0})
        r["n"] += 1
        r["total_pnl"] += float(t["pnl"])
        if float(t["pnl"]) > 0:
            r["wins"] += 1
    regimes = []
    for r in sorted(reg_rows.values(), key=lambda x: -x["n"]):
        r["win_rate"] = round(r["wins"] / r["n"] * 100, 1)
        r["total_pnl"] = round(r["total_pnl"], 2)
        regimes.append(r)

    # 5 · strategy decay — recent 30d expectancy vs prior 30d + weekly slope
    d30 = (now - timedelta(days=30)).isoformat()
    d60 = (now - timedelta(days=60)).isoformat()
    recent = [float(t["pnl"]) for t in trades if str(t["closed_at"]) >= d30]
    prior = [float(t["pnl"]) for t in trades if d60 <= str(t["closed_at"]) < d30]
    slope = None
    pnls = [w["avg_pnl"] for w in walk]
    if len(pnls) >= 3:
        n = len(pnls)
        xm, ym = (n - 1) / 2, sum(pnls) / n
        num = sum((i - xm) * (y - ym) for i, y in enumerate(pnls))
        den = sum((i - xm) ** 2 for i in range(n))
        slope = round(num / den, 3) if den else None
    r_avg = round(sum(recent) / len(recent), 2) if recent else None
    p_avg = round(sum(prior) / len(prior), 2) if prior else None
    if r_avg is None or p_avg is None:
        decay_status = "INSUFFICIENT_DATA"
    elif r_avg >= p_avg or (slope is not None and slope > 0):
        decay_status = "STABLE_OR_IMPROVING"
    elif r_avg < p_avg * 0.5 or r_avg < 0 <= p_avg:
        decay_status = "DECAYING"
    else:
        decay_status = "SOFTENING"
    decay = {"status": decay_status, "recent_30d_avg_pnl": r_avg,
             "recent_30d_n": len(recent), "prior_30d_avg_pnl": p_avg,
             "prior_30d_n": len(prior), "weekly_slope": slope}

    # 6 · execution attribution — gross vs commission/swap from broker deals
    gross = comm = swap = 0.0
    deal_n = 0
    async for d in db.broker_deals.find(
            {"user_id": user["id"], "received_at": {"$gte": since}},
            {"profit": 1, "commission": 1, "swap": 1}).limit(5000):
        deal_n += 1
        gross += float(d.get("profit") or 0)
        comm += float(d.get("commission") or 0)
        swap += float(d.get("swap") or 0)
    execution = {"deals": deal_n, "gross_pnl": round(gross, 2),
                 "commission": round(comm, 2), "swap": round(swap, 2),
                 "net_pnl": round(gross + comm + swap, 2),
                 "cost_drag_pct": (round(abs(comm + swap) / abs(gross) * 100, 1)
                                   if gross else None)}

    return {"generated_at": now.isoformat(), "window_days": int(days),
            "trades": len(trades), "calibration": cal, "symbols": symbols,
            "walk_forward": walk, "walk_forward_stability": stability,
            "regimes": regimes, "decay": decay, "execution": execution}


@router.get("/rr-watch")
async def rr_watch(user=Depends(get_current_user)):
    """Realized R:R watch — compares trade geometry before vs after the
    iter-119 expectancy fix (2026-07-08T09:45Z) so the improvement is
    verifiable at a glance. R:R per trade = |TP1 − entry| / |entry − SL|.
    """
    FIX_TS = "2026-07-08T09:45:00+00:00"
    db = get_db()

    async def _bucket(q: dict) -> dict:
        rrs, pnl, wins, n_closed = [], 0.0, 0, 0
        async for t in db.trades.find(
                {"user_id": user["id"], "origin": "auto", **q},
                {"entry_price": 1, "stop_loss": 1, "tp1": 1, "take_profit": 1,
                 "pnl": 1, "status": 1}).limit(1000):
            e = float(t.get("entry_price") or 0)
            sl = float(t.get("stop_loss") or 0)
            tp1 = float(t.get("tp1") or t.get("take_profit") or 0)
            if e and sl and tp1 and abs(e - sl) > 0:
                rrs.append(abs(tp1 - e) / abs(e - sl))
            if t.get("status") == "closed":
                n_closed += 1
                p = float(t.get("pnl") or 0)
                pnl += p
                if p > 0:
                    wins += 1
        rrs.sort()
        med = rrs[len(rrs) // 2] if rrs else None
        return {
            "trades": len(rrs),
            "median_rr": round(med, 2) if med is not None else None,
            "closed": n_closed,
            "win_rate": round(wins / n_closed * 100, 1) if n_closed else None,
            "net_pnl": round(pnl, 2),
        }

    before = await _bucket({"opened_at": {"$lt": FIX_TS}})
    after = await _bucket({"opened_at": {"$gte": FIX_TS}})
    return {
        "fix_deployed_at": FIX_TS,
        "before": before,
        "after": after,
        "target_median_rr": 0.83,
        "note": ("Post-fix trades should show median R:R ≥ 0.75 "
                 "(payoff guard clamps SL to 1.2× TP1; final R:R guard skips below 0.75). "
                 "Pre-fix median was 0.31."),
    }


@router.get("/attribution")
async def get_attribution(user=Depends(get_current_user)):
    """Full performance attribution across every dimension."""
    return await compute_attribution(user["id"])


@router.get("/sessions")
async def get_sessions(user=Depends(get_current_user)):
    """Per-session breakdown — Asia / London / Overlap / NY / Off-hours.

    Returns win-rate, avg-R, expectancy-R and P&L per UTC session window so
    the trader can see which session their edge actually lives in.
    """
    return await compute_sessions(user["id"])


@router.post("/sessions/suggest-action")
async def suggest_session_action(user=Depends(get_current_user)):
    """Inspect the session breakdown and propose a single config tweak the
    user can apply with one click. Returns:
        {
          "action":  "tighten_worst" | "no_action",
          "session": "<bucket key>",
          "field":   "min_confidence_override",
          "from":    int, "to": int,
          "rationale": "<plain english>",
        }
    Strategy: if at least one bucket has ≥5 trades AND a win-rate below 40%,
    suggest tightening min_confidence by +5 for that session (clamped 95).
    """
    from database import get_db
    sessions = await compute_sessions(user["id"])
    buckets = sessions.get("buckets") or []
    worst = None
    for b in buckets:
        if b["count"] < 5:
            continue
        if b["win_rate"] >= 40:
            continue
        if worst is None or b["win_rate"] < worst["win_rate"]:
            worst = b
    if not worst:
        return {"action": "no_action",
                "rationale": "All sessions either trade above 40% win-rate or have fewer than 5 sampled trades."}

    db = get_db()
    cfg = await db.bot_configs.find_one({"user_id": user["id"], "account_id": None})
    cur_min = int((cfg or {}).get("min_confidence_override") or 65)
    new_min = min(95, cur_min + 5)
    return {
        "action": "tighten_worst",
        "session": worst["key"],
        "field": "min_confidence_override",
        "from": cur_min,
        "to": new_min,
        "rationale": (f"{worst['key']} has {worst['count']} trades at {worst['win_rate']}% win-rate "
                      f"(avg-R {worst['avg_r']}, P&L ${worst['total_pnl']}). "
                      f"Raising min_confidence to {new_min} will reduce signal volume "
                      f"in this session and require stronger conviction to fire."),
    }


@router.post("/sessions/apply-action")
async def apply_session_action(payload: dict, user=Depends(get_current_user)):
    """Apply the suggested config tweak returned by `suggest-action`. Body:
        {field: "min_confidence_override", to: 70}
    Only writes the default bot_config (account_id=None). Per-account
    overrides remain editable via the existing Bot Config page.
    """
    from database import get_db
    field = payload.get("field")
    if field != "min_confidence_override":
        raise HTTPException(status_code=400, detail="Only min_confidence_override is supported")
    try:
        new_val = int(payload.get("to"))
    except Exception:
        raise HTTPException(status_code=400, detail="`to` must be an integer")
    new_val = max(50, min(95, new_val))
    db = get_db()
    res = await db.bot_configs.update_one(
        {"user_id": user["id"], "account_id": None},
        {"$set": {"min_confidence_override": new_val,
                  "updated_at": datetime.now(timezone.utc).isoformat()}},
    )
    if res.matched_count == 0:
        # Create the default config if it doesn't exist yet
        await db.bot_configs.insert_one({
            "user_id": user["id"],
            "account_id": None,
            "min_confidence_override": new_val,
            "active": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    return {"ok": True, "field": field, "to": new_val}


@router.get("/by-account")
async def get_by_account(user=Depends(get_current_user)):
    """Side-by-side aggregates per MT5 account — feeds the dashboard's
    Per-Account Comparison widget (winner highlighting, 30d P&L, win rate, etc).
    """
    return await per_account_stats(user["id"])


@router.get("/auto-tune")
async def get_auto_tune(user=Depends(get_current_user)):
    """Per-symbol auto-tuned min-confidence thresholds derived from closed trades."""
    db = get_db()
    cfg = await db.bot_configs.find_one({"user_id": user["id"]}) or {}
    risk = cfg.get("risk_level", "medium")
    symbols = cfg.get("symbols") or ["XAUUSD", "BTCUSD"]
    rows = await get_all_thresholds(user["id"], risk, symbols)
    return {
        "risk_level": risk,
        "symbols": symbols,
        "enabled": bool(cfg.get("auto_tune_enabled", True)),
        "thresholds": rows,
    }


@router.post("/auto-tune/refresh")
async def refresh_auto_tune(user=Depends(get_current_user)):
    """Invalidate cached thresholds — recomputed on next read."""
    invalidate_cache(user["id"])
    db = get_db()
    cfg = await db.bot_configs.find_one({"user_id": user["id"]}) or {}
    risk = cfg.get("risk_level", "medium")
    symbols = cfg.get("symbols") or ["XAUUSD", "BTCUSD"]
    rows = []
    for s in symbols:
        rows.append(await get_auto_threshold(user["id"], s, risk))
    return {"refreshed": True, "thresholds": rows}


@router.post("/learned-meta/retrain")
async def retrain_learned_meta(user=Depends(get_current_user)):
    """Retrain the local logistic-regression classifier on the latest closed
    trades. Returns the new artifact summary (or a reason if training was
    skipped due to insufficient data)."""
    res = await learned_retrain()
    return res


@router.get("/learned-meta")
async def get_learned_meta(user=Depends(get_current_user)):
    """Inspect the currently-active learned classifier."""
    art = await learned_artifact()
    if not art:
        return {"trained": False}
    # Don't expose mu/sd vectors — keep the response compact
    out = {k: art[k] for k in (
        "n_samples", "n_wins", "train_auc", "threshold",
        "trained_at", "feature_names",
    ) if k in art}
    out["trained"] = True
    # iter-52 · expose calibration stats so the UI can show Brier improvement
    calib = art.get("calibration") or {}
    if calib:
        out["calibration"] = {
            "applied": not calib.get("skipped"),
            "A": calib.get("A"), "B": calib.get("B"),
            "n": calib.get("n"),
            "brier_raw": calib.get("brier_raw"),
            "brier_calibrated": calib.get("brier_calibrated"),
            "converged": calib.get("converged"),
            "reason": calib.get("reason"),
        }
    return out


@router.get("/learned-meta/drift")
async def get_drift_status(user=Depends(get_current_user)):
    """ADWIN drift status across session buckets (iter-52)."""
    from drift_detector import check_drift
    from database import get_db
    from entitlements import enforce_feature
    await enforce_feature(user, "drift_auto_retrain")
    return await check_drift(get_db())


@router.post("/learned-meta/drift/check-now")
async def check_drift_now(user=Depends(get_current_user)):
    """Force a drift check (respects cooldown). Returns whether a retrain fired."""
    from drift_detector import maybe_trigger_retrain
    from database import get_db
    from entitlements import enforce_feature
    await enforce_feature(user, "drift_auto_retrain")
    return await maybe_trigger_retrain(get_db())
