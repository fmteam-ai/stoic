"""gold_trend_pulse · async orchestrator: data → bias (15-min, hysteresis) → entry on the last COMPLETED M5 bar →
filters → decision row (gtp_decisions). SHADOW decisions are labeled against broker M1 bars with full costs;
demo_live decisions (own live gate) become a main-pipeline signal so EVERY existing guard applies."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from gold_trend_pulse import rules as R
from pip_utils import pip_value_usd_per_lot

logger = logging.getLogger("gold_trend_pulse")
SYMBOL = "XAUUSD"
PIP_PRICE = 0.10
MODES = ("shadow", "demo_live")


def enabled_for(cfg: dict | None) -> bool:
    return bool((cfg or {}).get("gold_trend_pulse_enabled")) and (cfg or {}).get("enabled", True) is not False


def requested_mode(cfg: dict | None) -> str:
    m = str((cfg or {}).get("gold_trend_pulse_mode") or "shadow")
    return m if m in MODES else "shadow"


def _utc_day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------------------------------- data
async def load_bars(db, user_id: str, account: dict | None, now: datetime) -> dict:
    """{m15, h1, h4, m5 (completed), today_open, prior_day_mid, source} from the USER's own EA stream."""
    from intraday_features import load_user_candles
    out = {"m15": [], "h1": [], "h4": [], "m5": [], "m1": [], "today_open": None, "prior_day_mid": None, "source": None}
    doc = await load_user_candles(SYMBOL, user_id, "M15", last_n=800, max_age_min=30)
    m15 = list((doc or {}).get("bars") or [])
    out["m15"] = m15
    if m15:
        out["h1"], out["h4"] = R.resample(m15, 3600), R.resample(m15, 14400)
        d0 = int(_utc_day_start(now).timestamp())
        today = [b for b in m15 if int(b["t"]) >= d0]
        prior = [b for b in m15 if d0 - 86400 <= int(b["t"]) < d0]
        out["today_open"] = today[0]["o"] if today else None
        out["prior_day_mid"] = (max(b["h"] for b in prior) + min(b["l"] for b in prior)) / 2.0 if prior else None
    m1doc = await load_user_candles(SYMBOL, user_id, "M1", last_n=1600, max_age_min=10)
    m1 = list((m1doc or {}).get("bars") or [])
    if m1:
        out["source"] = "ea_m1"
    else:
        from deferred_shadow import m1_bars_from_ticks
        from routes.bridge_routes import broker_offset_sec
        off = broker_offset_sec(account or {})[0] if account else 0
        start = now.timestamp() - 26 * 3600
        cur = db.scalp_ticks.find({"user_id": user_id, "symbol": {"$regex": f"^{SYMBOL}"},
                                   "last_ms": {"$gt": int((start - R.BIAS_REFRESH_MIN * 60) * 1000)}},
                                  {"ticks": 1, "broker_offset_sec": 1}).sort("first_ms", 1)
        buckets: dict = {}
        async for batch in cur:
            o = batch.get("broker_offset_sec", off)
            m1_bars_from_ticks(batch.get("ticks") or [], start, now.timestamp(), o, buckets=buckets)
        m1 = [buckets[t] for t in sorted(buckets)]
        out["source"] = "scalp_ticks" if m1 else None
    out["m1"] = m1
    out["m5"] = R.completed(R.m5_from_m1(m1), 300, now.timestamp()) if m1 else []
    return out


async def usd_high_impact_events(cfg: dict | None) -> list:
    try:
        from economic_calendar import get_events
        return [e for e in await get_events() if str(e.get("country") or "").upper() == "USD"]
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------------------------------------- state / stats
async def _state(db, account_id: str) -> dict:
    return await db.gtp_state.find_one({"_id": account_id}) or {"_id": account_id}


async def recent_outcomes(db, account_id: str, mode: str, since: datetime | None = None) -> list:
    """Labeled outcomes in time order: shadow rows from gtp_decisions, live rows from closed trades."""
    if mode == "demo_live":
        q = {"account_id": account_id, "scope": R.ENGINE, "status": "closed", "closed_at": {"$ne": None}}
        if since:
            q["closed_at"] = {"$gte": since.isoformat()}
        rows = await db.trades.find(q, {"closed_at": 1, "pnl": 1, "r_multiple": 1, "entry_price": 1, "stop_loss": 1, "exit_price": 1, "action": 1}).sort("closed_at", 1).to_list(length=2000)
        out = []
        for t in rows:
            r = t.get("r_multiple")
            if r is None and t.get("entry_price") and t.get("stop_loss") and t.get("exit_price"):
                sl = abs(float(t["entry_price"]) - float(t["stop_loss"])) or 1e-9
                r = (float(t["exit_price"]) - float(t["entry_price"])) / sl * (1 if t.get("action") == "BUY" else -1)
            out.append({"r": float(r or 0), "pnl_usd": t.get("pnl"), "closed_at": t.get("closed_at")})
        return out
    q = {"account_id": account_id, "verdict": "shadow", "outcome.result": {"$exists": True}}
    if since:
        q["outcome.exit_at"] = {"$gte": since.isoformat()}
    rows = await db.gtp_decisions.find(q, {"outcome": 1}).sort("outcome.exit_at", 1).to_list(length=2000)
    return [{"r": (x["outcome"] or {}).get("r"), "pnl_usd": (x["outcome"] or {}).get("pnl_usd"), "closed_at": (x["outcome"] or {}).get("exit_at")} for x in rows]


async def live_gate(db, account_id: str) -> dict:
    stats = R.outcome_stats(await recent_outcomes(db, account_id, "shadow"))
    return {**R.live_gate(stats), "stats": stats}


async def effective_mode(db, cfg: dict, account_id: str) -> tuple[str, dict]:
    want = requested_mode(cfg)
    gate = await live_gate(db, account_id)
    if want == "demo_live" and not gate["eligible"]:
        return "shadow", gate
    return want, gate


async def card(db, user_id: str, account_id: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    st = await _state(db, account_id)
    cfg = await db.bot_configs.find_one({"user_id": user_id, "account_id": account_id}) or await db.bot_configs.find_one({"user_id": user_id}) or {}
    mode, gate = await effective_mode(db, cfg, account_id)
    out = {"engine": R.ENGINE, "symbol": SYMBOL, "enabled": enabled_for(cfg), "requested_mode": requested_mode(cfg), "mode": mode,
           "live_gate": gate, "bias": R.BiasTracker.from_dict(st.get("bias")).to_dict(), "last_eval": st.get("last_eval"),
           "regime": st.get("regime") or "NO_TRADE", "range": st.get("range"), "basket": st.get("basket"), "last_note": st.get("last_note"),
           "layering_enabled": layering_on(cfg),
           "today": {}, "week": {}}
    for key, since in (("today", _utc_day_start(now)), ("week", now - timedelta(days=7))):
        rows = await recent_outcomes(db, account_id, mode, since)
        out[key] = R.outcome_stats(rows)
        out[key]["decisions"] = await db.gtp_decisions.count_documents({"account_id": account_id, "created_at": {"$gte": since.isoformat()}})
    out["recent"] = await db.gtp_decisions.find({"account_id": account_id}, {"_id": 0}).sort("created_at", -1).to_list(length=20)
    ns = news_settings(cfg)
    out["sweep_runner_enabled"] = runner_on(cfg)
    out["news"] = {**ns, "next": R.next_news_event(await usd_high_impact_events(cfg), now, ns["events"], ns["release_min"]),
                   "recent": await db.gtp_news_events.find({"account_id": account_id}, {"_id": 0}).sort("t", -1).to_list(length=10)}
    return out


# ------------------------------------------------------------------------------------------- evaluate

def layering_on(cfg: dict | None) -> bool:
    return bool((cfg or {}).get("gold_trend_pulse_layering_enabled"))


def runner_on(cfg: dict | None) -> bool:
    return (cfg or {}).get("gold_trend_pulse_sweep_runner_enabled", True) is not False


def news_settings(cfg: dict | None) -> dict:
    """item 14 — news mode is part of the engine (ON inside an engine that is OFF by default); event list / PRE action /
    release window are per account."""
    cfg = cfg or {}
    ev = cfg.get("gold_trend_pulse_news_events")
    pa = cfg.get("gold_trend_pulse_news_pre_action")
    try:
        rel = int(cfg.get("gold_trend_pulse_news_release_min") or R.NEWS_RELEASE_MIN_DEFAULT)
    except (TypeError, ValueError):
        rel = R.NEWS_RELEASE_MIN_DEFAULT
    return {"enabled": cfg.get("gold_trend_pulse_news_mode_enabled", True) is not False,
            "pre_action": pa if pa in R.NEWS_PRE_ACTIONS else "protect", "release_min": max(1, min(30, rel)),
            "events": [str(x) for x in ev] if isinstance(ev, (list, tuple)) and ev else list(R.NEWS_EVENTS_DEFAULT)}


async def _news_event(db, row: dict, *, inc_trades: int = 0, actions: list | None = None, skip: str | None = None, decision_id: str | None = None) -> None:
    """Per-event report row (gtp_news_events): surprise, bias, first move, trades taken / skipped and why, PRE actions."""
    upd: dict = {"$set": {k: v for k, v in row.items() if v is not None}, "$setOnInsert": {"created_at": datetime.now(timezone.utc).isoformat(), "trades": 0}}
    if inc_trades:
        upd["$inc"] = {"trades": inc_trades}
        upd["$setOnInsert"].pop("trades")
    push = {}
    if actions:
        push["pre_actions"] = {"$each": actions}
    if skip:
        push["skipped"] = {"$each": [skip]}
    if decision_id:
        push["decision_ids"] = {"$each": [decision_id]}
    if push:
        upd["$push"] = push
    await db.gtp_news_events.update_one({"account_id": row["account_id"], "key": row["key"]}, upd, upsert=True)


async def _news_protect(db, live: list, shadow: list, price: float, policy: str, now: datetime, key: str) -> list:
    """PRE (T−15 → T): open GTP positions ≥ +0.5R → stop to breakeven+costs; else per policy (protect = keep stop,
    close, tighten to −0.5R). Live rows via the fenced MODIFY_SL / FULL_CLOSE paths; shadow rows adjusted in place."""
    from close_commands import request_close
    acts = []
    for t in live:
        if t.get("gtp_news_protected") == key:
            continue
        entry = float(t["entry_price"])
        sl_dist = abs(entry - float(t.get("stop_loss") or 0)) if t.get("stop_loss") else float((t.get("gold_trend_pulse") or {}).get("sl_dist") or 0)
        act = R.news_pre_action(t["action"], entry, sl_dist, price, policy)
        if act["action"] == "close":
            await request_close(db, {"_id": t["_id"]}, reason="gtp_news_pre_close", actor="gold_trend_pulse",
                                pending_modification={"type": "FULL_CLOSE", "reason": "gtp_news_pre_close", "requested_at": now.isoformat()})
        elif act["action"] in ("breakeven", "tighten"):
            from trade_manager import _free_slot_filter, scalp_breakeven_sl, stamp_pending_modification, tightened_sl
            new_sl = scalp_breakeven_sl(t) if act["action"] == "breakeven" else act["new_sl"]
            if tightened_sl(t, new_sl) is not None:
                await stamp_pending_modification(db, _free_slot_filter(t["_id"]), {"type": "MODIFY_SL", "new_sl": new_sl, "reason": f"gtp_news_pre_{act['action']}"},
                                                 {"scalp_exit_rule": f"gtp_news_pre_{act['action']}"})
                act["new_sl"] = new_sl
            else:
                act["action"] = "keep (stop already tighter)"
        await db.trades.update_one({"_id": t["_id"]}, {"$set": {"gtp_news_protected": key}})
        acts.append({"trade_id": str(t["_id"]), "at": now.isoformat(), **act})
    for d in shadow:
        if d.get("news_protected") == key:
            continue
        act = R.news_pre_action(d["direction"], float(d["entry"]), float(d["sl_dist"] or 0), price, policy)
        upd = {"news_protected": key, "news_pre_action": act}
        if act["action"] == "close":
            lab = label_with_bars(d, [{"t": int(now.timestamp()) // 60 * 60, "o": price, "h": price, "l": price, "c": price}], now, force_exit="news_pre_close")
            if lab:
                upd["outcome"] = lab
        elif act["action"] == "breakeven":
            upd["stop_loss"] = R.breakeven_level(d["direction"], float(d["entry"]), float(d.get("spread_at_entry") or 0), float(d.get("commission_price") or 0))
        elif act["action"] == "tighten":
            upd["stop_loss"] = act["new_sl"]
        await db.gtp_decisions.update_one({"_id": d["_id"]}, {"$set": upd})
        acts.append({"decision_id": d.get("decision_id"), "at": now.isoformat(), **act})
    return acts


def _spread_price(account: dict, spread_price: float | None) -> float:
    sp = float(spread_price if spread_price is not None else (((account.get("current_spreads") or {}).get(SYMBOL) or 0) * PIP_PRICE))
    return sp or 0.25


def _costs(account: dict, cfg: dict, pip_usd: float) -> float:
    try:
        cps = float(account.get("commission_usd_per_lot_side") or cfg.get("scalp_commission_usd_per_lot_side") or 0)
        return round(2 * cps / (pip_usd or 10.0) * PIP_PRICE, 3)
    except (TypeError, ValueError):
        return 0.0


async def _open_gtp(db, account_id: str) -> tuple[list, list]:
    live = await db.trades.find({"account_id": account_id, "scope": R.ENGINE, "status": {"$in": ["open", "pending"]}}).to_list(length=20)
    shadow = await db.gtp_decisions.find({"account_id": account_id, "verdict": "shadow", "outcome": None}).to_list(length=20)
    return live, shadow


async def _close_gtp_positions(db, live: list, shadow: list, reason: str, now: datetime, price: float, only=None) -> int:
    """Hand-over / invalidation close: live rows via the fenced FULL_CLOSE path (discretionary reason, close_watchdog
    applies), shadow rows labeled at the current price."""
    from close_commands import request_close
    n = 0
    for t in live:
        if only and not only(t.get("action")):
            continue
        await request_close(db, {"_id": t["_id"]}, reason=reason, actor="gold_trend_pulse",
                            pending_modification={"type": "FULL_CLOSE", "reason": reason, "requested_at": now.isoformat()})
        n += 1
    for d in shadow:
        if only and not only(d.get("direction")):
            continue
        lab = label_with_bars(d, [{"t": int(now.timestamp()) // 60 * 60, "o": price, "h": price, "l": price, "c": price}], now, force_exit=reason)
        if lab:
            await db.gtp_decisions.update_one({"_id": d["_id"]}, {"$set": {"outcome": lab}})
            n += 1
    return n


def _decision(now, user_id, account_id, mode, regime_name, tracker, cand, plan, spread, source, **extra) -> dict:
    return {"decision_id": uuid.uuid4().hex, "user_id": user_id, "account_id": account_id, "symbol": SYMBOL, "created_at": now.isoformat(),
            "mode": mode, "regime": regime_name, "bias": tracker.bias, "bias_reasons": tracker.reasons, "trigger": cand.get("trigger"),
            "direction": cand["direction"], "entry": cand["entry"], "swing": cand.get("swing"), "atr_m5": round(float(cand.get("atr") or 0), 3),
            "spread_at_entry": round(spread, 2), "tp_dist": plan.get("tp"), "sl_dist": plan.get("sl"), "take_profit": plan.get("take_profit"),
            "stop_loss": plan.get("stop_loss"), "time_stop_min": plan.get("time_stop_min", R.TIME_STOP_MIN), "bars_source": source,
            "reason": cand.get("reason"), "outcome": None, **extra}


def _signal_from(decision: dict) -> dict:
    return {"action": decision["direction"], "entry_price": decision["entry"], "stop_loss": decision["stop_loss"], "take_profit": decision["take_profit"],
            "sl_pips": round(decision["sl_dist"] / PIP_PRICE, 1), "tp_pips": round(decision["tp_dist"] / PIP_PRICE, 1), "trigger": decision.get("trigger") or decision["regime"],
            "decision_id": decision["decision_id"], "planned_risk_usd": decision.get("planned_risk_usd"), "planned_risk_pct": decision.get("planned_risk_pct"),
            "spread_at_entry": decision["spread_at_entry"], "bias": decision["bias"], "reason": decision.get("reason") or "", "regime": decision["regime"],
            "basket_id": decision.get("basket_id"), "leg": decision.get("leg"), "time_stop_min": decision.get("time_stop_min"),
            "setup_id": decision.get("setup_id"), "runner": bool(decision.get("runner")), "be_trigger_price": decision.get("be_trigger_price"),
            "sweep": decision.get("sweep"), "news": decision.get("news"), "sl_dist": decision.get("sl_dist")}


async def evaluate(db, cfg: dict, account: dict, now: datetime | None = None, *, spread_price: float | None = None) -> dict:
    """One pass for an account. Returns {mode, regime, bias, reasons, note, decision, signal}; `signal` is set only for a
    demo_live decision that passed every engine-level filter (the main pipeline's guards run after this)."""
    now = now or datetime.now(timezone.utc)
    account_id, user_id = str(account["_id"]), str(account.get("user_id") or cfg.get("user_id"))
    st = await _state(db, account_id)
    tracker = R.BiasTracker.from_dict(st.get("bias"))
    mode, gate = await effective_mode(db, cfg, account_id)
    res = {"mode": mode, "requested_mode": requested_mode(cfg), "live_gate": gate, "bias": tracker.bias, "reasons": tracker.reasons,
           "regime": st.get("regime") or "NO_TRADE", "range": st.get("range"), "basket": st.get("basket"), "note": None, "decision": None, "signal": None}
    data = await load_bars(db, user_id, account, now)
    price = data["m5"][-1]["c"] if data["m5"] else (data["m15"][-1]["c"] if data["m15"] else None)
    if price is None:
        res["note"] = "no fresh XAUUSD stream from the EA"
        return res
    if tracker.due(now):
        tracker.update(R.daily_bias(data["h4"], data["h1"], price, data["today_open"], data["prior_day_mid"]), now)
    res["bias"], res["reasons"] = tracker.bias, tracker.reasons
    upd = {"bias": tracker.to_dict(), "last_eval": now.isoformat(), "user_id": user_id, "bars_source": data["source"]}

    async def _done(note: str):
        res["note"] = note
        await db.gtp_state.update_one({"_id": account_id}, {"$set": {**upd, "last_note": note}}, upsert=True)
        return res

    if not data["m5"]:
        return await _done("no M1/tick stream to build M5 bars")
    live, shadow = await _open_gtp(db, account_id)
    last_t = int(data["m5"][-1]["t"])
    m5_closed = st.get("last_m5_t") != last_t
    # --- regime every 5 min (item 12)
    if m5_closed or not st.get("regime"):
        reg = R.regime(tracker.bias, data["m5"], data["m15"], now.timestamp())
        upd["regime"], upd["range"] = reg["regime"], reg["range"]
        if reg["regime"] != "RANGE" and st.get("regime") == "RANGE" and st.get("range"):
            brk = R.range_breakout(data["m5"][-1], st["range"])
            if brk:   # breakout: no new RANGE trades, close RANGE trades on the wrong side, TREND takes over once bias confirms
                rng_live = [t for t in live if (t.get("gold_trend_pulse") or {}).get("regime") == "RANGE"]
                rng_sh = [d for d in shadow if d.get("regime") == "RANGE"]
                n = await _close_gtp_positions(db, rng_live, rng_sh, "gtp_range_breakout", now, price, only=lambda a: R.wrong_side(a, brk))
                upd["last_breakout"] = {"at": now.isoformat(), "direction": brk, "closed": n}
    else:
        reg = {"regime": st.get("regime"), "range": st.get("range")}
    res["regime"], res["range"] = reg["regime"], reg["range"]
    # --- open basket management (item 11)
    basket = st.get("basket")
    if basket:
        inval = R.basket_invalidated(basket["direction"], tracker.bias, data["m15"][-1]["c"] if data["m15"] else None, basket["shared_stop"])
        if inval:
            n = await _close_gtp_positions(db, [t for t in live if (t.get("gold_trend_pulse") or {}).get("basket_id") == basket["id"]],
                                           [d for d in shadow if d.get("basket_id") == basket["id"]], "gtp_basket_invalidated", now, price)
            upd["basket"] = None
            upd["last_basket"] = {**basket, "closed_at": now.isoformat(), "closed": n, "why": inval}
            return await _done(inval)
        if not any((t.get("gold_trend_pulse") or {}).get("basket_id") == basket["id"] for t in live) and \
                not any(d.get("basket_id") == basket["id"] for d in shadow):
            upd["basket"] = None       # every leg closed on its own → basket done
            basket = None
    # --- sweep runner leg (item 13): the second ticket of a setup whose scalp leg was opened on the previous pass
    if mode == "demo_live":
        pend = await db.gtp_decisions.find_one({"account_id": account_id, "verdict": "live_pending"})
        if pend:
            age = now - datetime.fromisoformat(pend["created_at"])
            if age <= timedelta(minutes=5) and any((t.get("gold_trend_pulse") or {}).get("setup_id") == pend.get("setup_id") for t in live):
                await db.gtp_decisions.update_one({"_id": pend["_id"]}, {"$set": {"verdict": "live"}})
                pend["verdict"] = "live"
                res["decision"], res["signal"] = pend, _signal_from(pend)
                return await _done(f"SWEEP runner leg {pend['direction']} → {pend['take_profit']} (½ size, BE at the scalp TP)")
            if age > timedelta(minutes=5):
                await db.gtp_decisions.update_one({"_id": pend["_id"]}, {"$set": {"verdict": "skipped", "skip_reason": "scalp leg not opened within 5 min — runner dropped"}})
    # --- news mode (item 14): PRE protects / blocks, RELEASE blocks, POST trades the confirmed surprise bias
    events = await usd_high_impact_events(cfg)
    ns = news_settings(cfg)
    ph = R.news_phase(events, now, ns["events"], ns["release_min"]) if ns["enabled"] else None
    news_bias, news_pre_close, news_key, m5_post = None, None, None, []
    if ph:
        news_key = ph["key"]
        t_rel = datetime.fromtimestamp(ph["t"], tz=timezone.utc)
        ev_row = {"account_id": account_id, "user_id": user_id, "key": news_key, "kind": ph["kind"], "title": ph["title"], "t": ph["t"],
                  "time_utc": t_rel.isoformat(), "phase": ph["phase"], "actual": ph["event"].get("actual") or None, "forecast": ph["event"].get("forecast") or None}
        if ph["phase"] == "PRE":
            acts = await _news_protect(db, live, shadow, price, ns["pre_action"], now, news_key)
            await _news_event(db, ev_row, actions=acts or None)
            return await _done(f"NEWS PRE {ph['kind']} {t_rel:%H:%M} UTC — no new entries" + (f"; {len(acts)} position(s) protected ({ns['pre_action']})" if acts else ""))
        if ph["phase"] == "RELEASE":
            await _news_event(db, ev_row)
            return await _done(f"NEWS RELEASE {ph['kind']} — no entries until {t_rel + timedelta(minutes=ns['release_min']):%H:%M} UTC")
        m5_post = [b for b in data["m5"] if int(b["t"]) >= ph["t"]]
        nbias = R.fomc_bias(m5_post) if ph["kind"] in R.NEWS_RATE_EVENTS else R.news_surprise_bias(ph["event"], ph["kind"])
        ev_row.update({"surprise": nbias.get("surprise"), "surprise_pct": nbias.get("surprise_pct"), "news_bias": nbias.get("bias"), "bias_reason": nbias.get("reason")})
        if nbias["bias"] is None:
            await _news_event(db, ev_row)
            return await _done(f"NEWS POST {ph['kind']}: {nbias['reason']}")
        if nbias["bias"] == "NONE":
            await _news_event(db, ev_row)                     # fall back to the normal daily-bias path
        else:
            pre = R.pre_release_range(data["m5"], ph["t"])
            conf = R.news_confirmation(nbias["bias"], pre, m5_post, ph["t"])
            ev_row.update({"first_move": conf.get("first_move"), "confirmed": conf["confirmed"], "confirm_reason": conf["reason"]})
            await _news_event(db, ev_row)
            if conf["confirmed"] is None:
                return await _done(f"NEWS POST {ph['kind']} bias {nbias['bias']}: {conf['reason']}")
            if conf["confirmed"] is False:
                return await _done(f"NEWS POST {ph['kind']} bias {nbias['bias']} NOT confirmed — {conf['reason']} — no news trade")
            news_bias = nbias["bias"]
            before = [b for b in data["m5"] if int(b["t"]) < ph["t"]]
            news_pre_close = before[-1]["c"] if before else price
    if reg["regime"] == "NO_TRADE" and not news_bias:
        return await _done("regime NO_TRADE (bias NONE, no confirmed range) — no new trades")
    ok, why = R.session_ok(now)
    if not ok:
        return await _done(why)
    # --- daily limits / cooldown (shared by both modes; legs count as trades)
    outcomes = await recent_outcomes(db, account_id, mode, _utc_day_start(now))
    consec = 0
    for o in reversed(outcomes):
        if float(o.get("r") or 0) <= 0:
            consec += 1
        else:
            break
    equity = float(account.get("equity") or account.get("balance") or 0)
    day_pnl_pct = (sum(float(o.get("pnl_usd") or 0) for o in outcomes) / equity * 100) if equity else 0.0
    stop = R.daily_stop(consec, day_pnl_pct, len(outcomes))
    if stop:
        return await _done(stop)
    if outcomes and outcomes[-1].get("closed_at"):
        last = outcomes[-1]
        until = R.cooldown_until("loss" if float(last.get("r") or 0) <= 0 else "win", datetime.fromisoformat(str(last["closed_at"])))
        if until and now < until:
            return await _done(f"cooldown until {until:%H:%M:%S} UTC")
    nb = R.news_blackout(events, now)
    if nb and not news_bias:                     # the POST phase of the configured event is the news trade itself
        return await _done(nb)
    spread = _spread_price(account, spread_price)
    pip_usd = float(pip_value_usd_per_lot(SYMBOL, account.get("account_type"), price=price) or 0)
    comm = _costs(account, cfg, pip_usd)
    max_lot = float(cfg.get("max_lot_size") or 0) or None
    layering = layering_on(cfg) and reg["regime"] == "TREND"
    if (live or shadow) and not (layering and basket):
        return await _done("one open gold_trend_pulse position per account")

    async def _record(decision: dict, plan: dict, sizing: dict) -> dict:
        decision.update({"lots": sizing.get("lots"), "planned_risk_usd": sizing.get("planned_risk_usd", sizing.get("risk_usd")),
                         "planned_risk_pct": sizing.get("planned_risk_pct", R.RISK_PCT), "risk_usd": sizing.get("risk_usd"), "risk_pct": sizing.get("risk_pct"),
                         "sizing_ok": sizing["ok"], "sizing_reason": sizing.get("reason"), "commission_price": comm,
                         "verdict": "live" if (mode == "demo_live" and sizing["ok"]) else ("shadow" if sizing["ok"] else "skipped"),
                         "skip_reason": None if sizing["ok"] else sizing.get("reason")})
        await db.gtp_decisions.insert_one(dict(decision))
        res["decision"] = decision
        if decision["verdict"] == "live":
            res["signal"] = _signal_from(decision)
        return decision

    async def _skip(decision: dict, why: str):
        decision.update({"verdict": "skipped", "skip_reason": why})
        await db.gtp_decisions.insert_one(dict(decision))
        return await _done(why)

    # ================================================================ NEWS (item 14) — POST phase, confirmed surprise bias
    if news_bias:
        if not m5_closed:
            return await _done(f"NEWS POST {ph['kind']} bias {news_bias}: waiting for the next M5 close")
        upd["last_m5_t"] = last_t
        ev_doc = await db.gtp_news_events.find_one({"account_id": account_id, "key": news_key}) or {}
        taken = int(ev_doc.get("trades") or 0)
        if taken >= R.NEWS_MAX_TRADES_PER_EVENT:
            return await _done(f"NEWS POST {ph['kind']}: {taken}/{R.NEWS_MAX_TRADES_PER_EVENT} news trades taken for this event")
        cand = R.detect_news_entry(data["m5"], m5_post, news_bias, news_pre_close) or R.detect_sweep_reclaim(data["m5"], news_bias)
        if not cand:
            return await _done(f"NEWS POST {ph['kind']} bias {news_bias}: no pullback / sweep-and-reclaim on the last M5 close")
        atr_post = R.atr(m5_post, 14) if len(m5_post) >= 3 else R.atr(data["m5"], 14)
        cand["atr"] = atr_post
        if cand["trigger"] == "sweep":
            cand["trigger"] = "news_sweep"
        plan = R.plan_news_exit(cand["direction"], cand["entry"], cand["swing"], atr_post)
        decision = _decision(now, user_id, account_id, mode, "NEWS", tracker, cand, plan, spread, data["source"], bias=news_bias,
                             news={"key": news_key, "kind": ph["kind"], "title": ph["title"], "bias": news_bias, "surprise": ev_row.get("surprise"), "n": taken + 1})
        if not plan["ok"]:
            await _news_event(db, {"account_id": account_id, "key": news_key}, skip=plan["skip"])
            return await _skip(decision, plan["skip"])
        ok, why = R.spread_ok(spread, plan["tp"])
        if not ok:
            await _news_event(db, {"account_id": account_id, "key": news_key}, skip=why)
            return await _skip(decision, why)
        sizing = R.size_lots(equity, plan["sl"], pip_usd, PIP_PRICE, max_lot=max_lot)
        d = await _record(decision, plan, sizing)
        await _news_event(db, {"account_id": account_id, "key": news_key}, inc_trades=1 if d["verdict"] in ("shadow", "live") else 0,
                          skip=None if d["verdict"] in ("shadow", "live") else d.get("skip_reason"), decision_id=d["decision_id"])
        return await _done(f"NEWS {d['verdict']} {cand['direction']} ({cand['trigger']}) TP {plan['tp']:.2f} SL {plan['sl']:.2f} · {ph['kind']} {taken + 1}/{R.NEWS_MAX_TRADES_PER_EVENT}")

    # ================================================================ RANGE (item 12) — acts on the last COMPLETED M1 bar
    if reg["regime"] == "RANGE":
        m1 = R.completed(data["m1"], 60, now.timestamp())
        if not m1:
            return await _done("RANGE: no M1 bars")
        if st.get("last_m1_t") == int(m1[-1]["t"]):
            return await _done("RANGE: waiting for the next M1 close")
        upd["last_m1_t"] = int(m1[-1]["t"])
        if (st.get("last_breakout") or {}).get("at") and reg["range"] is None:
            return await _done("RANGE: breakout — no new range trades")
        cand = R.detect_range_entry(m1[-1], reg["range"])
        if not cand:
            return await _done("RANGE: no edge rejection on the last M1 close")
        cand["atr"] = reg["range"]["atr"]
        plan = R.plan_range_exit(cand["direction"], cand["entry"], reg["range"])
        decision = _decision(now, user_id, account_id, mode, "RANGE", tracker, cand, plan, spread, data["source"], range=reg["range"])
        if not plan["ok"]:
            return await _skip(decision, plan["skip"])
        ok, why = R.spread_ok(spread, plan["tp"])
        if not ok:
            return await _skip(decision, why)
        sizing = R.size_lots(equity, plan["sl"], pip_usd, PIP_PRICE, max_lot=max_lot)
        d = await _record(decision, plan, sizing)
        return await _done(f"RANGE {d['verdict']} {cand['direction']} TP {plan['tp']:.2f} SL {plan['sl']:.2f}")

    # ================================================================ TREND (items 2/3/11) — acts on the last COMPLETED M5 bar
    if not m5_closed:
        return await _done("waiting for the next M5 close")
    upd["last_m5_t"] = last_t
    vwap = R.session_vwap(data["m5"], int(_utc_day_start(now).timestamp()))
    if layering:
        direction = "BUY" if tracker.bias == "UP" else "SELL"
        closes = [b["c"] for b in data["m5"]]
        ema20, a14 = R.ema(closes, 20)[-1], R.atr(data["m5"], 14)
        bar = data["m5"][-1]
        if basket is None:
            if len(outcomes) >= R.DAILY_MAX_LEGS:
                return await _done(f"daily cap: {len(outcomes)}/{R.DAILY_MAX_LEGS} legs")
            d0 = int(_utc_day_start(now).timestamp())
            stop = R.basket_shared_stop(direction, [b for b in data["m15"] if int(b["t"]) >= d0])
            if stop is None:
                return await _done("LAYERING: no M15 swing structure today for the shared stop")
            basket = {"id": uuid.uuid4().hex[:12], "direction": direction, "shared_stop": stop, "opened_at": now.isoformat(),
                      "levels": R.layer_levels(direction, ema20, vwap, a14), "legs": [], "risk_used_pct": 0.0}
        pending = [(leg, lvl) for leg, lvl in basket["levels"] if leg not in [x["leg"] for x in basket["legs"]]]
        hit = next(((leg, lvl) for leg, lvl in pending if R.leg_touched(direction, bar, lvl)), None)
        if hit is None:
            upd["basket"] = basket if basket["legs"] else None
            return await _done(f"LAYERING: no leg level touched ({', '.join(f'{l} {v}' for l, v in pending)})")
        leg, lvl = hit
        if len(outcomes) + len(basket["legs"]) >= R.DAILY_MAX_LEGS:
            return await _done(f"daily cap: {R.DAILY_MAX_LEGS} legs")
        entry = bar["c"]
        tp = max(R.TP_MIN, min(R.TP_MAX, R.TP_ATR_MULT * a14))
        sgn = 1 if direction == "BUY" else -1
        plan = {"ok": True, "tp": round(tp, 2), "sl": round(abs(entry - basket["shared_stop"]), 2), "take_profit": round(entry + sgn * tp, 2),
                "stop_loss": basket["shared_stop"], "time_stop_min": R.TIME_STOP_MIN}
        cand = {"direction": direction, "entry": entry, "trigger": leg, "atr": a14, "reason": f"layered pullback leg {leg} at {lvl}"}
        decision = _decision(now, user_id, account_id, mode, "TREND", tracker, cand, plan, spread, data["source"], basket_id=basket["id"], leg=leg,
                             shared_stop=basket["shared_stop"], layered=True)
        ok, why = R.spread_ok(spread, plan["tp"])
        if not ok:
            return await _skip(decision, why)
        sizing = R.size_basket_leg(equity, entry, basket["shared_stop"], pip_usd, PIP_PRICE, float(basket.get("risk_used_pct") or 0), max_lot=max_lot)
        d = await _record(decision, plan, sizing)
        if sizing["ok"]:
            basket["legs"].append({"leg": leg, "decision_id": d["decision_id"], "lots": sizing["lots"], "entry": entry, "at": now.isoformat()})
            basket["risk_used_pct"] = round(float(basket.get("risk_used_pct") or 0) + float(sizing.get("risk_pct") or 0), 3)
        else:
            basket["legs"].append({"leg": leg, "skipped": sizing.get("reason"), "at": now.isoformat()})
        upd["basket"] = basket
        res["basket"] = basket
        return await _done(f"LAYERING {d['verdict']} {leg} {direction} @ {entry} shared stop {basket['shared_stop']} ({sizing.get('reason') or 'sized'})")
    cand = R.detect_sweep_reclaim(data["m5"], tracker.bias) or R.detect_entry(data["m5"], tracker.bias, vwap)
    if cand is None:
        return await _done("no sweep-and-reclaim / pullback / micro-breakout on the last M5 close")
    if cand.get("blocked"):
        return await _done(cand["blocked"])
    if cand["trigger"] == "sweep":                      # item 13 — scalp leg (+ optional ½-size runner to the pre-drop high)
        plan = R.plan_sweep_exit(cand["direction"], cand["entry"], cand["sweep_low"], cand["pre_drop_high"], cand["atr"], runner=runner_on(cfg))
        setup_id = uuid.uuid4().hex[:12]
        sweep = {k: cand[k] for k in ("sweep_low", "swept_level", "pre_drop_high", "drop", "sweep_t")}
        decision = _decision(now, user_id, account_id, mode, "TREND", tracker, cand, plan, spread, data["source"], sweep=sweep, setup_id=setup_id,
                             basket_id=setup_id if plan.get("runner") else None)
        if not plan["ok"]:
            return await _skip(decision, plan["skip"])
        ok, why = R.spread_ok(spread, plan["tp"])
        if not ok:
            return await _skip(decision, why)
        sizing = R.size_lots(equity, plan["sl"], pip_usd, PIP_PRICE, max_lot=max_lot)
        runner_lots = None
        if sizing["ok"] and plan.get("runner"):
            scalp_lots, runner_lots = R.split_runner_lots(sizing["lots"])
            if runner_lots:
                per_lot = sizing["risk_usd"] / sizing["lots"]
                sizing = {**sizing, "lots": scalp_lots, "risk_usd": round(per_lot * scalp_lots, 2), "risk_pct": round(per_lot * scalp_lots / equity * 100, 3) if equity else None}
        d = await _record(decision, plan, sizing)
        if runner_lots and sizing["ok"]:
            rp = plan["runner"]
            rplan = {"ok": True, "tp": rp["tp"], "sl": plan["sl"], "take_profit": rp["take_profit"], "stop_loss": plan["stop_loss"], "time_stop_min": rp["time_stop_min"]}
            rcand = {**cand, "trigger": "sweep_runner", "reason": "runner leg to the pre-drop high — stop to breakeven+costs once the scalp TP is hit"}
            rdec = _decision(now, user_id, account_id, mode, "TREND", tracker, rcand, rplan, spread, data["source"], sweep=sweep, setup_id=setup_id,
                             basket_id=setup_id, runner=True, be_trigger_price=rp["be_trigger_price"])
            rdec.update({"lots": runner_lots, "planned_risk_usd": round(per_lot * runner_lots, 2), "planned_risk_pct": round(per_lot * runner_lots / equity * 100, 3) if equity else None,
                         "risk_usd": round(per_lot * runner_lots, 2), "risk_pct": round(per_lot * runner_lots / equity * 100, 3) if equity else None,
                         "sizing_ok": True, "commission_price": comm, "verdict": "live_pending" if mode == "demo_live" else "shadow", "skip_reason": None})
            await db.gtp_decisions.insert_one(dict(rdec))
            res["runner"] = rdec
        return await _done(f"SWEEP {d['verdict']} {cand['direction']} TP {plan['tp']:.2f} SL {plan['sl']:.2f}" + (f" + runner → {plan['runner']['take_profit']}" if runner_lots else ""))
    plan = R.plan_exit(cand["direction"], cand["entry"], cand["swing"], cand["atr"])
    decision = _decision(now, user_id, account_id, mode, "TREND", tracker, cand, plan, spread, data["source"], rsi7=cand["rsi"], ema20=cand["ema20"], vwap=vwap)
    if not plan["ok"]:
        return await _skip(decision, plan["skip"])
    ok, why = R.spread_ok(spread, plan["tp"])
    if not ok:
        return await _skip(decision, why)
    sizing = R.size_lots(equity, plan["sl"], pip_usd, PIP_PRICE, max_lot=max_lot)
    d = await _record(decision, plan, sizing)
    return await _done(f"TREND {d['verdict']} {cand['direction']} trigger {cand['trigger']} TP {plan['tp']:.2f} SL {plan['sl']:.2f}")


# ------------------------------------------------------------------------------------------- shadow labeling
def label_with_bars(decision: dict, bars: list, now: datetime, force_exit: str | None = None) -> dict | None:
    """Walk M1 bars after the decision: SL-first when both touched, BE (entry ± costs) once 60 % of TP is reached,
    45-min time-stop at the bar close. Costs: spread at entry + commission. Returns the outcome or None (pending)."""
    d = decision
    direction, entry = d["direction"], float(d["entry"])
    sgn = 1 if direction == "BUY" else -1
    tp, sl = float(d["take_profit"]), float(d["stop_loss"])
    spread, comm = float(d.get("spread_at_entry") or 0), float(d.get("commission_price") or 0)
    be = R.breakeven_level(direction, entry, spread, comm)
    opened = datetime.fromisoformat(d["created_at"])
    ts_min = int(d.get("time_stop_min") or R.TIME_STOP_MIN)
    mfe = mae = 0.0
    exit_px = exit_reason = exit_at = None
    if force_exit and bars:
        exit_px, exit_reason, exit_at = bars[-1]["c"], force_exit, now
        bars = []
    for b in bars:
        t = datetime.fromtimestamp(int(b["t"]), tz=timezone.utc)
        if t < opened:
            continue
        hi, lo = (b["h"] - entry) * sgn, (b["l"] - entry) * sgn
        mfe, mae = max(mfe, hi), min(mae, lo)
        hit_sl = (b["l"] <= sl) if sgn > 0 else (b["h"] >= sl)
        hit_tp = (b["h"] >= tp) if sgn > 0 else (b["l"] <= tp)
        if hit_sl:
            exit_px, exit_reason = sl, ("breakeven" if sl == be else "stop_loss")
        elif hit_tp:
            exit_px, exit_reason = tp, "take_profit"
        elif (t + timedelta(minutes=1)) - opened >= timedelta(minutes=ts_min):
            exit_px, exit_reason = b["c"], "time_stop"
        if exit_px is not None:
            exit_at = t + timedelta(minutes=1)
            break
        be_px = d.get("be_trigger_price")                 # runner leg: BE once the scalp TP is hit; else 60 % of TP
        reached = ((b["h"] >= float(be_px)) if sgn > 0 else (b["l"] <= float(be_px))) if be_px else \
            R.breakeven_due(direction, entry, float(d["tp_dist"]), b["h"] if sgn > 0 else b["l"])
        if reached and sl != be:
            sl = be
    if exit_px is None:
        if now - opened > timedelta(minutes=ts_min + 10) and bars:
            exit_px, exit_reason, exit_at = bars[-1]["c"], "time_stop", datetime.fromtimestamp(int(bars[-1]["t"]), tz=timezone.utc)
        else:
            return None
    gross = (exit_px - entry) * sgn
    net = gross - spread - comm
    sl_dist = float(d["sl_dist"]) or 1e-9
    lots = float(d.get("lots") or 0.01)
    pip_usd = pip_value_usd_per_lot(SYMBOL, None, price=entry) or 10.0
    return {"result": "win" if net > 0 else "loss", "exit_price": exit_px, "exit_reason": exit_reason, "exit_at": exit_at.isoformat(),
            "gross_price": round(gross, 2), "net_price": round(net, 2), "r": round(net / sl_dist, 3), "mfe": round(mfe, 2), "mae": round(mae, 2),
            "pnl_usd": round(net / PIP_PRICE * pip_usd * lots, 2), "time_in_trade_min": round((exit_at - opened).total_seconds() / 60, 1),
            "be_armed": sl == be, "label_version": 1}


async def label_pending(db, now: datetime | None = None) -> dict:
    from deferred_shadow import m1_bars_after
    now = now or datetime.now(timezone.utc)
    out = {"checked": 0, "labeled": 0}
    cur = db.gtp_decisions.find({"verdict": "shadow", "outcome": None, "created_at": {"$lte": (now - timedelta(minutes=2)).isoformat()}})
    async for d in cur:
        out["checked"] += 1
        bars = await m1_bars_after(db, d["user_id"], SYMBOL, datetime.fromisoformat(d["created_at"]).timestamp() - 60)
        lab = label_with_bars(d, bars, now)
        if lab:
            await db.gtp_decisions.update_one({"_id": d["_id"]}, {"$set": {"outcome": lab}})
            out["labeled"] += 1
    return out


# ------------------------------------------------------------------------------------------- main-pipeline signal
def pipeline_signal(sig: dict, base: dict) -> dict:
    """Shape a GTP live decision as a main-pipeline signal: single TP on all three legs (no re-split, no rescale),
    fixed 0.25 % risk cap, deterministic scope so the AI-pipeline model gates stay advisory."""
    return {**base, "action": sig["action"], "chart_action": sig["action"], "confidence": 70.0,
            "entry_price": sig["entry_price"], "stop_loss": sig["stop_loss"], "take_profit": sig["take_profit"],
            "tp1": sig["take_profit"], "tp2": sig["take_profit"], "tp3": sig["take_profit"],
            "sl_pips": sig["sl_pips"], "tp_pips": [sig["tp_pips"]] * 3, "lot_size": 0.01, "kelly_f": 0.0,
            "effective_risk_pct": R.RISK_PCT, "risk_amount": sig["planned_risk_usd"], "risk_pct_cap": R.RISK_PCT,
            "scope": R.ENGINE, "strategy_engine": R.ENGINE, "engine": R.ENGINE, "engine_label": "GOLD TREND PULSE · XAUUSD M5 pullback scalper",
            "entry_style": "trend", "reasoning": f"GOLD TREND PULSE: bias {sig['bias']}, trigger {sig['trigger']} — {sig['reason']}",
            "key_factors": [f"daily bias {sig['bias']}", f"trigger {sig['trigger']}", f"TP {sig['tp_pips']} pips / SL {sig['sl_pips']} pips"],
            "gold_trend_pulse": {k: sig.get(k) for k in ("trigger", "decision_id", "planned_risk_usd", "planned_risk_pct", "spread_at_entry", "bias",
                                                         "regime", "basket_id", "leg", "time_stop_min", "setup_id", "runner", "be_trigger_price",
                                                         "sweep", "news", "sl_dist")},
            "rr_ratio": round(sig["tp_pips"] / sig["sl_pips"], 2) if sig["sl_pips"] else None,
            "engine_geometry": {"stop_loss": sig["stop_loss"], "take_profit": sig["take_profit"], "tp1": sig["take_profit"], "tp2": sig["take_profit"], "tp3": sig["take_profit"]},
            "veto_applied": False, "tradeable": True, "cheap_hold": False, "created_at": datetime.now(timezone.utc)}
