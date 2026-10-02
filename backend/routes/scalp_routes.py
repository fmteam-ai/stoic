"""Scalp subsystem API — config, status, decisions, metrics, retrain."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth import get_current_user
from database import get_db
from route_utils import parse_object_id

router = APIRouter(prefix="/scalp", tags=["scalp"])


@router.get("/session-window")
async def get_session_window(symbol: str = "EURUSD",
                             user=Depends(get_current_user)):
    """iter-174 — effective trading-session window (UTC hours) with the
    instrument default and any per-user override."""
    from scalp.instruments import approved
    sym = (symbol or "EURUSD").upper()
    cfg = approved(sym)
    if cfg is None:
        raise HTTPException(status_code=404, detail="symbol not approved")
    db = get_db()
    ov = await db.scalp_session_windows.find_one({"_id": f"{user['id']}:{sym}"})
    return {"symbol": sym,
            "start_utc": int(ov["start_utc"]) if ov else cfg.session_start_utc,
            "end_utc": int(ov["end_utc"]) if ov else cfg.session_end_utc,
            "default_start_utc": cfg.session_start_utc,
            "default_end_utc": cfg.session_end_utc,
            "override": bool(ov)}


@router.post("/session-window")
async def set_session_window(payload: dict, request: Request,
                             user=Depends(get_current_user)):
    """iter-174 — set (or reset) the per-user session window. Bounds:
    0 <= start < end <= 24 UTC. Audited; permissions cache invalidated so
    the change takes effect on the next tick."""
    from scalp.instruments import approved
    sym = str(payload.get("symbol") or "EURUSD").upper()
    cfg = approved(sym)
    if cfg is None:
        raise HTTPException(status_code=404, detail="symbol not approved")
    db = get_db()
    if payload.get("reset"):
        await db.scalp_session_windows.delete_one(
            {"_id": f"{user['id']}:{sym}"})
        s, e = cfg.session_start_utc, cfg.session_end_utc
    else:
        from security import rate_limit
        await rate_limit(db, "scalp_session_window", user["id"], 20, 600,
                         request=request)
        try:
            s, e = int(payload.get("start_utc")), int(payload.get("end_utc"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422,
                                detail="start_utc and end_utc must be "
                                       "integer UTC hours")
        if not (0 <= s < e <= 24):
            raise HTTPException(status_code=422,
                                detail="window must satisfy 0 <= start < "
                                       "end <= 24 (UTC hours)")
        await db.scalp_session_windows.update_one(
            {"_id": f"{user['id']}:{sym}"},
            {"$set": {"user_id": user["id"], "symbol": sym,
                      "start_utc": s, "end_utc": e,
                      "updated_at":
                          datetime.now(timezone.utc).isoformat()}},
            upsert=True)
    from scalp import permissions
    permissions.invalidate(user["id"], sym)
    from step_up import audit_event
    await audit_event(db, user["id"], "scalp_session_window",
                      {"symbol": sym, "start_utc": s, "end_utc": e,
                       "reset": bool(payload.get("reset"))}, request)
    return {"ok": True, "symbol": sym, "start_utc": s, "end_utc": e,
            "override": not bool(payload.get("reset"))}


class ScalpConfigRequest(BaseModel):
    account_id: str
    symbol: str = "EURUSD"
    enabled: bool = False
    mode: str = "shadow"          # shadow | demo_live
    confirm_live: bool = False
    commission_usd_per_lot_side: float = 0.0


@router.post("/config")
async def set_config(req: ScalpConfigRequest, user=Depends(get_current_user)):
    from scalp.instruments import approved
    if approved(req.symbol) is None:
        raise HTTPException(status_code=422,
                            detail=f"{req.symbol} is not in the approved scalp universe (EURUSD only)")
    if req.mode not in ("shadow", "demo_live"):
        raise HTTPException(status_code=422, detail="mode must be shadow or demo_live")
    if req.mode == "demo_live" and not req.confirm_live:
        raise HTTPException(status_code=422,
                            detail="demo_live requires confirm_live=true — orders WILL be sent to the broker")
    db = get_db()
    account = await db.accounts.find_one({"_id": parse_object_id(req.account_id, "Account"),
                                          "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    from scalp.engine import apply_config
    runner = await apply_config(db, account, req.symbol, req.enabled, req.mode,
                                commission_usd_per_lot_side=req.commission_usd_per_lot_side)
    return {"ok": True, "status": runner.status()}


@router.get("/exec-calibration")
async def exec_calibration(user=Depends(get_current_user)):
    """review item 6 — empirical calibration table: execution-score buckets
    vs realised outcomes, so gate thresholds can be set from data."""
    db = get_db()
    buckets = [(0, 39), (40, 59), (60, 79), (80, 100)]
    rows = [{"bucket": f"{lo}-{hi}", "n": 0, "live_submitted": 0,
             "broker_rejected": 0, "history_capped": 0, "labeled": 0,
             "net_pips_sum": 0.0, "target_first": 0} for lo, hi in buckets]
    cur = db.scalp_decisions.find(
        {"user_id": user["id"], "execution_quality.score": {"$exists": True}},
        {"execution_quality": 1, "dataset": 1, "outcome": 1})
    async for d in cur:
        s = int((d.get("execution_quality") or {}).get("score") or 0)
        row = rows[min(3, max(0, (0 if s < 40 else 1 if s < 60
                                  else 2 if s < 80 else 3)))]
        row["n"] += 1
        ds = d.get("dataset") or ""
        if ds == "submitted":
            row["live_submitted"] += 1
        elif ds == "attempt_failed":
            row["broker_rejected"] += 1
        if (d.get("execution_quality") or {}).get("history_capped"):
            row["history_capped"] += 1
        out = d.get("outcome") or {}
        if out.get("result"):
            row["labeled"] += 1
            row["net_pips_sum"] += float(out.get("net_pips") or 0)
            if out.get("result") == "target_first":
                row["target_first"] += 1
    for r in rows:
        r["avg_net_pips"] = (round(r.pop("net_pips_sum") / r["labeled"], 3)
                             if r["labeled"] else None)
        r["target_first_rate"] = (round(r["target_first"] / r["labeled"], 3)
                                  if r["labeled"] else None)
        att = r["live_submitted"] + r["broker_rejected"]
        r["reject_rate"] = (round(r["broker_rejected"] / att, 3)
                            if att else None)
    from scalp.exec_quality import thresholds_snapshot
    return {"buckets": rows, "thresholds": thresholds_snapshot()}


@router.get("/broker-stats")
async def broker_stats_summary(broker: str, user=Depends(get_current_user)):
    """Per-session broker execution behaviour learned from real fills."""
    from scalp.broker_stats import summary
    return await summary(get_db(), broker)


@router.get("/review")
async def scalp_review(user=Depends(get_current_user)):
    """Iter-156 · Fast-Scalp review: execution heatmap (dow×hour), gate
    effectiveness (what each veto avoided, shadow-labeled), per-broker
    calibration and cost/latency attribution."""
    from datetime import datetime, timezone
    db = get_db()

    def _stage(d):
        if d.get("reject_stage"):
            return d["reject_stage"]
        g = d.get("gates") or {}
        for name in ("permission", "edge", "risk", "final"):
            if (g.get(name) or {}).get("ok") is False:
                return name
        return "other"

    heat, gates = {}, {}
    costs = {"labeled": 0, "gross_pips": 0.0, "spread_pips": 0.0,
             "slippage_pips": 0.0, "commission_pips": 0.0, "net_pips": 0.0}
    times = []
    async for d in db.scalp_decisions.find(
            {"user_id": user["id"]},
            {"ts_ms": 1, "verdict": 1, "reject_stage": 1, "gates": 1,
             "outcome": 1}).sort("ts_ms", -1).limit(2000):
        try:
            ts = datetime.fromtimestamp((d.get("ts_ms") or 0) / 1000,
                                        tz=timezone.utc)
        except Exception:
            continue
        out = d.get("outcome") or {}
        labeled = bool(out.get("result"))
        net = float(out.get("net_pips") or 0)

        key = (ts.weekday(), ts.hour)
        h = heat.setdefault(key, {"dow": ts.weekday(), "hour": ts.hour,
                                  "n": 0, "labeled": 0, "net_pips": 0.0})
        h["n"] += 1
        if labeled:
            h["labeled"] += 1
            h["net_pips"] += net

        if d.get("verdict") == "rejected":
            st = _stage(d)
            g = gates.setdefault(st, {"stage": st, "n": 0, "labeled": 0,
                                      "net_sum": 0.0, "target_first": 0})
            g["n"] += 1
            if labeled:
                g["labeled"] += 1
                g["net_sum"] += net
                if out.get("result") == "target_first":
                    g["target_first"] += 1

        if labeled:
            costs["labeled"] += 1
            costs["gross_pips"] += float(out.get("gross_move_pips") or 0)
            costs["spread_pips"] += float(out.get("spread_cost_pips") or 0)
            costs["slippage_pips"] += float(out.get("exit_slippage_pips") or 0)
            costs["commission_pips"] += float(out.get("commission_pips") or 0)
            costs["net_pips"] += net
            if out.get("time_to_exit_ms") is not None:
                times.append(float(out["time_to_exit_ms"]))

    heatmap = sorted(heat.values(), key=lambda x: (x["dow"], x["hour"]))
    for h in heatmap:
        h["net_pips"] = round(h["net_pips"], 2)
        h["avg_net_pips"] = (round(h["net_pips"] / h["labeled"], 2)
                             if h["labeled"] else None)

    gate_rows = []
    for g in sorted(gates.values(), key=lambda x: -x["n"]):
        g["avoided_pips"] = round(-g["net_sum"], 2) if g["labeled"] else None
        g["avg_net_pips"] = (round(g["net_sum"] / g["labeled"], 2)
                             if g["labeled"] else None)
        g["target_first_rate"] = (round(g["target_first"] / g["labeled"], 2)
                                  if g["labeled"] else None)
        g.pop("net_sum", None)
        gate_rows.append(g)

    for k in ("gross_pips", "spread_pips", "slippage_pips",
              "commission_pips", "net_pips"):
        costs[k] = round(costs[k], 2)

    times.sort()

    def _p(p):
        return (round(times[min(len(times) - 1, int(p * len(times)))] / 1000, 1)
                if times else None)

    latency = {"labeled": len(times), "time_to_exit_p50_s": _p(0.50),
               "time_to_exit_p95_s": _p(0.95)}

    brokers = []
    seen = set()
    async for a in db.accounts.find({"user_id": user["id"],
                                     "status": {"$ne": "deleted"}},
                                    {"broker": 1}).limit(20):
        b = a.get("broker")
        if not b or b in seen:
            continue
        seen.add(b)
        try:
            from scalp.broker_stats import summary
            brokers.append({"broker": b, **(await summary(db, b))})
        except Exception:
            pass
        if len(brokers) >= 3:
            break

    return {"heatmap": heatmap, "gate_effectiveness": gate_rows,
            "cost_attribution": costs, "latency": latency,
            "brokers": brokers}


@router.delete("/config")
async def remove_config(account_id: str, symbol: str = "EURUSD",
                        user=Depends(get_current_user)):
    """Remove a runner from the Scalp page entirely (tombstoned so incoming
    EA ticks for this account/symbol are ignored until re-enabled)."""
    db = get_db()
    account = await db.accounts.find_one({"_id": parse_object_id(account_id, "Account"),
                                          "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    sym = (symbol or "EURUSD").upper()
    from scalp.engine import _runners
    runner = _runners.get(f"{account_id}:{sym}")
    if runner is not None and len(runner.live_trades) > 0:
        raise HTTPException(status_code=409,
                            detail="Runner has open live scalps — close them before removing")
    await db.scalp_configs.update_one(
        {"account_id": account_id, "symbol": sym},
        {"$set": {"user_id": user["id"], "enabled": False, "mode": "shadow",
                  "removed": True}},
        upsert=True)
    _runners.pop(f"{account_id}:{sym}", None)
    return {"ok": True, "removed": f"{account_id}:{sym}"}


@router.get("/status")
async def status(account_id: str = None, user=Depends(get_current_user)):
    db = get_db()
    from scalp.engine import _runners, get_runner
    # hydrate runners from persisted configs so status survives restarts
    q = {"user_id": user["id"]}
    if account_id:
        q["account_id"] = account_id
    async for cfg in db.scalp_configs.find(q):
        if cfg.get("removed"):
            continue
        r = get_runner(cfg["account_id"], cfg["user_id"], cfg["symbol"])
        if r is not None and not r.enabled and cfg.get("enabled"):
            r.enabled = True
            r.mode = cfg.get("mode", "shadow")
    out = []
    from scalp import permissions as _perms
    from state_contract import effective_connection_state
    from bson import ObjectId
    for r in _runners.values():
        if r.user_id != user["id"]:
            continue
        if account_id and r.account_id != account_id:
            continue
        # iter-175 — permissions normally refresh on incoming ticks; also
        # kick a (throttled, non-blocking) refresh on status reads so the
        # panel shows current regime/session even when the stream is down.
        _perms.maybe_refresh(db, r.user_id, r.symbol, r.cfg)
        st = r.status()
        # iter-176 — tick-ingress + terminal facts so the UI can diagnose
        # an offline stream precisely.
        ing = await db.scalp_tick_ingress.find_one(
            {"_id": f"{r.account_id}:{r.symbol}"})
        if ing:
            st["ingress"] = {k: ing.get(k) for k in
                             ("last_batch_at", "last_status",
                              "last_reason", "last_ticks", "batches")}
        else:
            st["ingress"] = None
        try:
            acc = await db.accounts.find_one(
                {"_id": ObjectId(r.account_id)})
        except Exception:  # noqa: BLE001
            acc = None
        if acc:
            conn = effective_connection_state(acc)
            st["terminal"] = {
                "ea_version": acc.get("ea_version"),
                "heartbeat_age_seconds": conn.get("heartbeat_age_seconds"),
                "connected": conn.get("connected")}
        else:
            st["terminal"] = None
        out.append(st)
    from scalp.engine import audit_backlog
    return {"runners": out, "audit": audit_backlog()}


@router.get("/executions")
async def scalp_executions(limit: int = 25, account_id: str = None,
                           user=Depends(get_current_user)):
    """Iter-154 · execution metadata for scalp-originated orders: intent /
    reservation / order / deal / position identifiers, broker latency,
    lifecycle timeline and protection state."""
    from datetime import datetime, timezone
    db = get_db()
    q = {"user_id": user["id"], "scope": "scalp_fast"}
    if account_id:
        q["account_id"] = account_id
    n = min(max(int(limit), 1), 100)
    now = datetime.now(timezone.utc)

    def _dt(v):
        try:
            d = v if hasattr(v, "tzinfo") and not isinstance(v, str) \
                else datetime.fromisoformat(str(v))
            return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
        except Exception:
            return None

    items = []
    async for t in db.trades.find(q).sort("_id", -1).limit(n):
        tid = str(t["_id"])
        did = t.get("scalp_decision_id")
        resv = None
        if did:
            resv = await db.risk_reservations.find_one(
                {"decision_id": did}, {"reservation_id": 1, "state": 1,
                                       "risk_usd": 1})
        ev_q = ({"$or": [{"decision_id": did}, {"trade_id": tid}]}
                if did else {"trade_id": tid})
        timeline = []
        async for e in db.trade_events.find(
                ev_q, {"event_type": 1, "occurred_at": 1}).sort(
                "ts_ms", 1).limit(40):
            timeline.append({"type": e.get("event_type"),
                             "at": e.get("occurred_at")})

        latency_ms = None
        d0, d1 = _dt(t.get("_dispatched_at")), _dt(t.get("opened_at"))
        if d0 and d1:
            ms = (d1 - d0).total_seconds() * 1000
            if 0 <= ms < 3_600_000:
                latency_ms = int(ms)

        lc = str(t.get("lifecycle_state") or "")
        protected = bool(t.get("stop_loss")) and lc not in (
            "FILLED_UNPROTECTED", "PROTECTION_REQUESTED")
        unprotected_age = None
        if t.get("status") == "open" and not protected and d1:
            unprotected_age = int((now - d1).total_seconds())

        items.append({
            "trade_id": tid,
            "symbol": t.get("symbol"),
            "action": t.get("action"),
            "status": t.get("status"),
            "lifecycle_state": lc or None,
            "submission_state": t.get("submission_state"),
            "intent_id": (t.get("intent_id")
                          or (t.get("pending_modification") or {}).get("intent_id")),
            "decision_id": did,
            "reservation_id": (resv or {}).get("reservation_id"),
            "reservation_state": (resv or {}).get("state"),
            "reserved_risk_usd": (resv or {}).get("risk_usd"),
            "order_ticket": t.get("mt5_ticket"),
            "deal_id": t.get("broker_deal_id"),
            "position_id": t.get("position_ticket") or t.get("position_id"),
            "broker_latency_ms": latency_ms,
            "protection": {"protected": protected,
                           "state": t.get("protection_state") or lc or None,
                           "unprotected_age_sec": unprotected_age},
            "reconciliation": ("estimated" if t.get("pnl_estimated")
                               else "unknown" if t.get("pnl_unknown")
                               else "backfilled" if t.get("backfilled_at")
                               else "reconciled"),
            "opened_at": str(t.get("opened_at")) if t.get("opened_at") else None,
            "closed_at": str(t.get("closed_at")) if t.get("closed_at") else None,
            "pnl": t.get("pnl"),
            "timeline": timeline,
        })
    return {"items": items, "generated_at": now.isoformat()}


@router.get("/decisions")
async def decisions(limit: int = 50, symbol: str = None,
                    user=Depends(get_current_user)):
    db = get_db()
    q = {"user_id": user["id"]}
    if symbol:
        q["symbol"] = symbol.upper()
    docs = await db.scalp_decisions.find(q).sort("ts_ms", -1).to_list(min(limit, 200))
    for d in docs:
        d["id"] = str(d.pop("_id"))
    return {"decisions": docs}


@router.get("/metrics")
async def metrics(symbol: str = "EURUSD", account_id: str = None,
                  user=Depends(get_current_user)):
    """Step 18 production metrics: alpha / execution / risk / stability.

    Round 14 — the response is an explicitly-labelled ROLLING sample (last
    5000 outcomes) plus lifetime aggregates and a block-bootstrap CI on net
    expectancy; the model is resolved by the runner's broker/account-type
    model key when account_id is provided (artifacts are broker-specific)."""
    db = get_db()
    q = {"user_id": user["id"], "symbol": symbol.upper(),
         "outcome.result": {"$in": ["target_first", "stop_first", "timeout"]}}
    if account_id:
        q["account_id"] = account_id
    docs = await db.scalp_decisions.find(q).sort("ts_ms", -1).to_list(5000)
    n = len(docs)
    if n == 0:
        return {"symbol": symbol.upper(), "n": 0}
    tf = [d for d in docs if d["outcome"]["result"] == "target_first"]
    sf = [d for d in docs if d["outcome"]["result"] == "stop_first"]
    # Review item 1: outcome.net_pips is ALREADY fully cost-loaded
    # (spread + entry/exit slippage + commission) — never subtract expected
    # costs a second time. gross_move_pips is the mid-to-mid reference.
    net = [float(d["outcome"].get("net_pips") or 0) for d in docs]
    gross = [float(d["outcome"].get("gross_move_pips",
                                    d["outcome"].get("net_pips") or 0)) for d in docs]
    costs = [float(d.get("cost_pips") or 0) for d in docs]
    wins = [p for p in net if p > 0]
    losses = [p for p in net if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    resolved_dir = len(tf) + len(sf)
    # Round 14 item 5 / Round 15 item 6 — model artifacts are broker/
    # account-type specific: derive the model key from the ACCOUNT DOCUMENT
    # (survives restarts and runner-less accounts); the in-memory runner is
    # only used for the cached commission check.
    model_doc = None
    model_scope = "symbol_only"
    runner = None
    acc = None
    if account_id:
        from protection_guard import find_account
        from scalp import model as scalp_model
        from scalp.engine import _runners
        runner = _runners.get(f"{account_id}:{symbol.upper()}")
        acc, _why = await find_account(db, account_id)
        if acc is not None:
            model_scope = scalp_model.make_key(
                str(acc.get("broker") or ""),
                str(acc.get("account_type") or ""), symbol.upper())
            model_doc = await db.scalp_models.find_one(
                {"model_key": model_scope})
    if model_doc is None and not account_id:
        model_doc = await db.scalp_models.find_one({"symbol": symbol.upper()})
    tick_count = await db.scalp_ticks.count_documents({"user_id": user["id"],
                                                       "symbol": symbol.upper()})
    # Round 14 item 6 / Round 15 item 7 — lifetime aggregates over a
    # CANONICAL resolved population: numeric net_pips, resolved outcomes
    # only (q already pins outcome.result), no partial/administrative docs.
    lifetime = None
    lifetime_match = {**q, "outcome.net_pips": {"$type": "number"}}
    async for g in db.scalp_decisions.aggregate([
            {"$match": lifetime_match},
            {"$group": {"_id": None, "n": {"$sum": 1},
                        "net_sum": {"$sum": "$outcome.net_pips"}}}]):
        lifetime = {"n": int(g["n"]),
                    "net_expectancy_pips": round(
                        float(g["net_sum"] or 0) / max(1, int(g["n"])), 3),
                    "population": ("resolved outcomes (target_first/"
                                   "stop_first/timeout) with numeric "
                                   "outcome.net_pips")}
    # Round 14 item 7 / Round 15 item 8 — bootstrap CIs SPLIT by execution
    # mode: shadow sims and broker fills have different cost/fill
    # distributions; only broker outcomes support executable-profit claims.
    from scalp.stats import block_bootstrap_ci
    net_ci95 = block_bootstrap_ci(list(reversed(net)))

    def _mode_stats(datasets):
        sub = [float((d.get("outcome") or {}).get("net_pips") or 0)
               for d in docs if d.get("dataset") in datasets]
        return {"n": len(sub),
                "net_expectancy_pips": (round(sum(sub) / len(sub), 3)
                                        if sub else None),
                "ci95": block_bootstrap_ci(list(reversed(sub)))}

    expectancy_by_mode = {
        "shadow": _mode_stats(("candidate",)),
        "broker_fills": {**_mode_stats(("filled_live",)),
                         "account_type": (acc or {}).get("account_type")},
    }
    # Round 16 — expectancy sliced by MARKET REGIME at decision time: an
    # edge that only exists in one regime must never be extrapolated to all.
    regime_nets: dict = {}
    for d in docs:
        reg = (((d.get("gates") or {}).get("permission") or {})
               .get("regime")) or "UNKNOWN"
        regime_nets.setdefault(reg, []).append(
            float((d.get("outcome") or {}).get("net_pips") or 0))
    expectancy_by_regime = {
        reg: {"n": len(v),
              "net_expectancy_pips": round(sum(v) / len(v), 3),
              "ci95": block_bootstrap_ci(list(reversed(v)))}
        for reg, v in sorted(regime_nets.items())}
    # Round 15 item 9 — latency decomposition percentiles (rolling window)
    def _pct(vals, p):
        if not vals:
            return None
        vs = sorted(vals)
        return vs[min(len(vs) - 1, int(p / 100 * len(vs)))]

    d2s = [int(d["decision_to_submit_ms"]) for d in docs
           if d.get("decision_to_submit_ms") is not None]
    s2a = [int(d["submit_to_ack_ms"]) for d in docs
           if d.get("submit_to_ack_ms") is not None]
    latency = {
        "decision_to_submit_ms": {"n": len(d2s), "p50": _pct(d2s, 50),
                                  "p95": _pct(d2s, 95), "p99": _pct(d2s, 99)},
        "submit_to_broker_ack_ms": {"n": len(s2a), "p50": _pct(s2a, 50),
                                    "p95": _pct(s2a, 95),
                                    "p99": _pct(s2a, 99)},
        "note": ("EA poll latency is on trade docs (_dispatched_at); "
                 "P95/P99 matter more than averages for scalping"),
    }
    # Round 17 item 10 — distributed submission-capacity operational view.
    from datetime import datetime, timedelta, timezone
    from scalp.engine import (MAX_BROKER_CONCURRENT_SUBMISSIONS,
                              _slot_metrics, capacity_integrity_reason)
    now_dt = datetime.now(timezone.utc)
    now_iso = now_dt.isoformat()
    active_slots = 0
    oldest_slot_age_sec = None
    async for s in db.scalp_submission_slots.find(
            {"lease_until": {"$gte": now_iso}, "token": {"$ne": None}},
            {"acquired_at": 1}):
        active_slots += 1
        try:
            age = int((now_dt - datetime.fromisoformat(
                s["acquired_at"])).total_seconds())
            if oldest_slot_age_sec is None or age > oldest_slot_age_sec:
                oldest_slot_age_sec = age
        except (KeyError, TypeError, ValueError):
            pass
    pending_without_slot = await db.trades.count_documents(
        {"scope": "scalp_fast", "status": "pending",
         "submission_slot": {"$exists": False}})
    cap_rejections = []
    day_ago_ms = int((now_dt - timedelta(hours=24)).timestamp() * 1000)
    async for g in db.scalp_decisions.aggregate([
            {"$match": {"ts_ms": {"$gte": day_ago_ms},
                        "reject_stage": {"$in": [
                            "submission_capacity",
                            "submission_capacity_broker",
                            "capacity_integrity"]}}},
            {"$group": {"_id": {"account_id": "$account_id",
                                "symbol": "$symbol"},
                        "n": {"$sum": 1}}},
            {"$sort": {"n": -1}}, {"$limit": 20}]):
        cap_rejections.append({"account_id": g["_id"].get("account_id"),
                               "symbol": g["_id"].get("symbol"),
                               "n": int(g["n"])})
    capacity = {
        "pool_size_per_broker": MAX_BROKER_CONCURRENT_SUBMISSIONS,
        "active_leased_slots": active_slots,
        "oldest_slot_age_sec": oldest_slot_age_sec,
        "pending_trades_without_slot": pending_without_slot,
        "integrity": capacity_integrity_reason(),
        "lifecycle": dict(_slot_metrics),
        "capacity_rejections_24h": cap_rejections,
    }
    return {
        "symbol": symbol.upper(), "n": n,
        "window": {
            "type": "rolling", "max_samples": 5000, "n": n,
            "from_ts_ms": docs[-1].get("ts_ms"),
            "to_ts_ms": docs[0].get("ts_ms"),
            "note": ("rolling sample of the most recent outcomes — see "
                     "`lifetime` for all-time aggregates"),
        },
        "lifetime": lifetime,
        "expectancy_by_mode": expectancy_by_mode,
        "expectancy_by_regime": expectancy_by_regime,
        "latency": latency,
        "capacity": capacity,
        "commission_check": (runner.commission_check
                             if runner is not None else None),
        "alpha": {
            "target_before_stop_rate": round(len(tf) / resolved_dir, 3) if resolved_dir else None,
            "gross_expectancy_pips": round(sum(gross) / n, 3),
            "net_expectancy_pips": round(sum(net) / n, 3),
            "net_expectancy_ci95_pips": net_ci95,
            "stressed_net_expectancy_pips": round(
                (sum(net) - 0.5 * sum(costs)) / n, 3),
            "avg_winner_pips": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loser_pips": round(sum(losses) / len(losses), 2) if losses else None,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
            "timeout_rate": round(sum(1 for d in docs if d["outcome"]["result"] == "timeout") / n, 3),
        },
        "execution": {
            "avg_cost_pips": round(sum(costs) / n, 2),
            "avg_spread_pips": round(sum(float(d["forecast"]["expected_spread_cost_pips"])
                                         for d in docs) / n, 2),
        },
        "model": ({"oos_auc": model_doc.get("oos_auc"), "n_samples": model_doc.get("n_samples"),
                   "usable": model_doc.get("usable"), "trained_at": model_doc.get("trained_at"),
                   "model_scope": model_scope}
                  if model_doc else {"model_scope": model_scope,
                                     "note": ("no model artifact for this "
                                              "scope — pass account_id for "
                                              "broker-specific resolution")}),
        "data": {"tick_batches_recorded": tick_count},
        "verdicts": {
            "shadow_traded": sum(1 for d in docs if d.get("verdict") == "shadow_traded"),
            "live_traded": sum(1 for d in docs if d.get("verdict") == "live_traded"),
            "rejected": sum(1 for d in docs if d.get("verdict") == "rejected"),
        },
    }


@router.post("/retrain")
async def retrain(symbol: str = "EURUSD", account_id: str = None,
                  user=Depends(get_current_user)):
    from scalp.model import retrain as do_retrain
    db = get_db()
    broker, account_type = "any", "any"
    if account_id:
        acc = await db.accounts.find_one({"_id": parse_object_id(account_id, "Account"),
                                          "user_id": user["id"]})
        if acc:
            broker = str(acc.get("broker") or "any")
            account_type = str(acc.get("account_type") or "any")
    return await do_retrain(db, symbol.upper(), broker=broker,
                            account_type=account_type)
