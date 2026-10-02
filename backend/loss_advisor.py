"""Auto Loss Review — iter-54.

Aggregate, automatic analysis of recent losing trades. On top of the per-loss
post-mortems (loss_postmortem.py), this module periodically reviews ALL exact
losses of the last window together with current market state, asks Claude for
concrete counter-measures from a FIXED menu, then SHADOW-TESTS each measure
against the user's actual recent trades:

    "would have avoided $X of losses / missed $Y of wins → net $Z"

so every suggestion ships with hard evidence (today's lesson: a plausible
guardrail can silently kill a winning strategy). With Daily Auto-Learning ON
(default), measures that pass the strict evidence bar are auto-applied as live
guards and auto-reverted when fresh evidence turns negative.

Collections:
  • `loss_reviews` — one doc per review run (auto or manual)
  • `auto_guards`  — evidence-gated live guards (apply / revert audit trail)
"""
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from bson import ObjectId

from pip_utils import base_symbol
from regime_adapter import velocity_veto
from ws_manager import manager as ws_manager
from typing import Optional

from pydantic import BaseModel, Field

import llm_client
from llm_models import finite_float, SWEEP_TIMEOUT_S

logger = logging.getLogger("loss-advisor")

WINDOW_DAYS = 7          # losses analysed
SHADOW_DAYS = 14         # trades replayed for evidence
MIN_NEW_LOSSES = 1       # iter-55 · daily learning: any new loss triggers a review after cooldown
COOLDOWN_HOURS = 24      # min gap between auto reviews per user

# iter-55 · Daily Auto-Learning — evidence-gated auto-apply (user-consented).
AUTO_APPLY_MIN_NET = 100.0   # $ net effect over the shadow window
AUTO_APPLY_RATIO = 2.0       # losses_avoided must be ≥ 2× wins_missed
GATE_TYPES = {"min_confidence", "velocity_veto", "session_block", "symbol_pause"}
MAX_ACTIVE_GUARDS = 5

_ADVISOR_SYSTEM = """You are the head of risk for an algorithmic gold/index/crypto
trading desk. You receive aggregate statistics of the bot's recent LOSING trades
(exact broker data only), loss-pattern clusters, per-segment performance, and
the current market state. Propose the most effective counter-measures to
minimise FUTURE losses without destroying the strategy's winning segments.

Return STRICT JSON only:
{
  "diagnosis": "2-3 sentence root-cause diagnosis of where losses concentrate",
  "market_context": "1-2 sentences on how CURRENT market conditions affect the risk",
  "measures": [
    {
      "title": "short imperative title",
      "rationale": "why this helps, referencing the data",
      "priority": "high|medium|low",
      "type": "min_confidence|velocity_veto|session_block|symbol_pause|friday_flat|other",
      "params": {}
    }
  ]
}

Allowed `type` values and their `params`:
- "min_confidence": {"symbol": "XAUUSD"|null, "value": <50-95>}         — raise the min-confidence gate
- "velocity_veto":  {"regime": "LOW_VOL_TREND", "velocity_counter_max": <num>, "velocity_veto_threshold": <num>}
- "session_block":  {"session": "asia"|"london"|"ny"|"off", "symbol": null|"XAUUSD", "action": null|"BUY"|"SELL"}
- "symbol_pause":   {"symbol": "US30"}
- "friday_flat":    {"mode": "close"|"tighten", "minutes_before": <int>}
- "other":          {}  (free-form ideas that don't map to a knob)

Max 5 measures, ordered by expected impact. Prefer measures that target the
loss clusters WITHOUT touching profitable segments. No markdown, JSON only."""


# ---------------------------------------------------------------- gathering
async def _gather(db, user_id: str) -> dict:
    """Exact-data trades of the shadow window, joined to their signals."""
    since_shadow = (datetime.now(timezone.utc) - timedelta(days=SHADOW_DAYS)).isoformat()
    since_loss = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()
    trades = await db.trades.find({
        "user_id": user_id,
        "status": "closed",
        "origin": "auto",
        "closed_at": {"$gte": since_shadow},
        "pnl": {"$ne": None},
        "pnl_estimated": {"$ne": True},
        "pnl_unknown": {"$ne": True},
    }).to_list(length=2000)
    sig_ids = []
    for t in trades:
        sid = t.get("signal_id")
        if sid:
            try:
                sig_ids.append(ObjectId(sid))
            except Exception:
                pass
    signals = {}
    if sig_ids:
        async for s in db.signals.find({"_id": {"$in": sig_ids}}):
            signals[str(s["_id"])] = s
    losses = [t for t in trades
              if float(t.get("pnl") or 0) < 0 and (t.get("closed_at") or "") >= since_loss]
    return {"trades": trades, "signals": signals, "losses": losses,
            "since_loss": since_loss, "since_shadow": since_shadow}


def _sig_of(t: dict, ds: dict) -> dict:
    return ds["signals"].get(str(t.get("signal_id") or "")) or {}


def _seg(t: dict, ds: dict) -> tuple:
    s = _sig_of(t, ds)
    regime = s.get("regime")
    regime = (regime.get("regime") if isinstance(regime, dict) else regime) or "?"
    sess = s.get("session")
    sess = (sess.get("primary") if isinstance(sess, dict) else sess) or "off"
    return (base_symbol(t.get("symbol")), t.get("action") or "?", regime, str(sess).lower())


def _crossed_weekend(t: dict) -> bool:
    try:
        o = datetime.fromisoformat(str(t.get("opened_at")))
        c = datetime.fromisoformat(str(t.get("closed_at")))
        d = o.date()
        while d <= c.date():
            if d.weekday() == 5:  # crossed a Saturday
                return True
            d += timedelta(days=1)
    except (ValueError, TypeError):
        pass
    return False


def _aggregates(ds: dict) -> dict:
    segs: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    reasons: dict = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    weekend_losses = 0.0
    for t in ds["trades"]:
        pnl = float(t.get("pnl") or 0)
        key = "|".join(_seg(t, ds))
        segs[key]["n"] += 1
        segs[key]["wins"] += pnl > 0
        segs[key]["pnl"] += pnl
    for t in ds["losses"]:
        pnl = float(t.get("pnl") or 0)
        r = (t.get("close_reason") or "unknown").split("+")[0]
        reasons[r]["n"] += 1
        reasons[r]["pnl"] += pnl
        if _crossed_weekend(t):
            weekend_losses += pnl
    seg_rows = [
        {"segment": k, "n": v["n"], "win_rate": round(100 * v["wins"] / v["n"], 1),
         "pnl": round(v["pnl"], 2)}
        for k, v in segs.items() if v["n"] >= 2
    ]
    seg_rows.sort(key=lambda r: r["pnl"])
    total_pnl = round(sum(float(t.get("pnl") or 0) for t in ds["trades"]), 2)
    loss_pnl = round(sum(float(t.get("pnl") or 0) for t in ds["losses"]), 2)
    return {
        "window_days": WINDOW_DAYS,
        "shadow_days": SHADOW_DAYS,
        "trades": len(ds["trades"]),
        "losses": len(ds["losses"]),
        "total_pnl": total_pnl,
        "loss_pnl": loss_pnl,
        "segments_worst_first": seg_rows[:12],
        "loss_reasons": {k: {"n": v["n"], "pnl": round(v["pnl"], 2)} for k, v in reasons.items()},
        "weekend_gap_loss_pnl": round(weekend_losses, 2),
    }


# ------------------------------------------------------------- sanitising
_MEASURE_TYPES = {"min_confidence", "velocity_veto", "session_block",
                  "symbol_pause", "friday_flat", "other"}
_SESSIONS = {"asia", "london", "ny", "off"}
# An auto-applied guard may not block more than this share of the shadow
# window's trades — a "guard" that blocks almost everything is a shutdown.
AUTO_APPLY_MAX_BLOCK_SHARE = 0.4


def _clean_symbol(v):
    if v is None:
        return None
    v = str(v).strip().upper()
    return v if 0 < len(v) <= 20 and v.replace(".", "").replace("-", "").replace("_", "").isalnum() else None


def _sanitize_measure(m) -> dict | None:
    """Validate + clamp one LLM-proposed measure. None = drop it.

    The LLM's output can be auto-applied as a live guard, so every param is
    coerced into the documented range; nothing out of range is trusted.
    """
    if not isinstance(m, dict):
        return None
    mtype = m.get("type")
    if mtype not in _MEASURE_TYPES:
        return None
    p = m.get("params") if isinstance(m.get("params"), dict) else {}
    clean: dict = {}
    if mtype == "min_confidence":
        clean["value"] = finite_float(p.get("value"), 50.0, 95.0, float("nan"))
        if clean["value"] != clean["value"]:   # NaN → unusable
            return None
        clean["symbol"] = _clean_symbol(p.get("symbol"))
    elif mtype == "velocity_veto":
        regime = str(p.get("regime") or "LOW_VOL_TREND").upper()
        if not regime.replace("_", "").isalpha() or len(regime) > 40:
            return None
        clean["regime"] = regime
        for k in ("velocity_counter_max", "velocity_veto_threshold"):
            v = finite_float(p.get(k), 0.0, 1e6, float("nan"))
            if v != v or v <= 0:
                return None
            clean[k] = v
    elif mtype == "session_block":
        sess = str(p.get("session") or "").lower()
        if sess not in _SESSIONS:
            return None
        clean["session"] = sess
        clean["symbol"] = _clean_symbol(p.get("symbol"))
        act = p.get("action")
        clean["action"] = act if act in ("BUY", "SELL") else None
    elif mtype == "symbol_pause":
        clean["symbol"] = _clean_symbol(p.get("symbol"))
        if not clean["symbol"]:
            return None
    elif mtype == "friday_flat":
        mode = p.get("mode")
        clean["mode"] = mode if mode in ("close", "tighten") else "tighten"
        clean["minutes_before"] = int(finite_float(p.get("minutes_before"), 5, 240, 60))
    out = {k: v for k, v in m.items() if k not in ("params", "evidence")}
    out["type"] = mtype
    out["params"] = clean
    return out


# ------------------------------------------------------------- shadow tests
def _predicate(measure: dict):
    """Build predicate(trade, signal) → bool for testable measure types."""
    mtype = measure.get("type")
    p = measure.get("params") or {}
    if mtype == "min_confidence":
        val = float(p.get("value") or 0)
        sym = p.get("symbol")

        def pred(t, s):
            if sym and base_symbol(t.get("symbol")) != base_symbol(sym):
                return False
            conf = s.get("confidence")
            return conf is not None and float(conf) < val
        return pred
    if mtype == "velocity_veto":
        regime = (p.get("regime") or "LOW_VOL_TREND").upper()
        cfg = {"regime_overrides": {regime: {
            "velocity_counter_max": p.get("velocity_counter_max"),
            "velocity_veto_threshold": p.get("velocity_veto_threshold"),
        }}}

        def pred(t, s):
            return bool(s) and velocity_veto(s, cfg) is not None
        return pred
    if mtype == "session_block":
        sess = str(p.get("session") or "").lower()
        sym = p.get("symbol")
        act = p.get("action")

        def pred(t, s):
            if sym and base_symbol(t.get("symbol")) != base_symbol(sym):
                return False
            if act and t.get("action") != act:
                return False
            ts = s.get("session")
            ts = (ts.get("primary") if isinstance(ts, dict) else ts) or "off"
            return str(ts).lower() == sess
        return pred
    if mtype == "symbol_pause":
        sym = p.get("symbol")

        def pred(t, s):
            return bool(sym) and base_symbol(t.get("symbol")) == base_symbol(sym)
        return pred
    if mtype == "friday_flat":
        return lambda t, s: _crossed_weekend(t)
    return None


def _shadow_test(measure: dict, ds: dict) -> dict | None:
    pred = _predicate(measure)
    if pred is None:
        return None
    blocked = 0
    losses_avoided = wins_missed = 0.0
    for t in ds["trades"]:
        try:
            if not pred(t, _sig_of(t, ds)):
                continue
        except Exception:
            continue
        pnl = float(t.get("pnl") or 0)
        blocked += 1
        if pnl < 0:
            losses_avoided += -pnl
        else:
            wins_missed += pnl
    return {
        "testable": True,
        "shadow_days": SHADOW_DAYS,
        "trades_blocked": blocked,
        "trades_total": len(ds["trades"]),
        "losses_avoided": round(losses_avoided, 2),
        "wins_missed": round(wins_missed, 2),
        "net_effect": round(losses_avoided - wins_missed, 2),
    }


# -------------------------------------------------------------------- LLM
class _MeasureParams(BaseModel):
    """Union of every measure type's params (all optional). Values are NOT
    trusted — ``_sanitize_measure`` re-validates and clamps each one."""
    symbol: Optional[str] = None
    value: Optional[float] = None
    regime: Optional[str] = None
    velocity_counter_max: Optional[float] = None
    velocity_veto_threshold: Optional[float] = None
    session: Optional[str] = None
    action: Optional[str] = None
    mode: Optional[str] = None
    minutes_before: Optional[float] = None


class _MeasureOut(BaseModel):
    title: str = ""
    rationale: str = ""
    priority: str = "medium"
    type: str
    params: _MeasureParams = Field(default_factory=_MeasureParams)


class AdvisorOut(BaseModel):
    diagnosis: str = ""
    market_context: str = ""
    measures: list[_MeasureOut] = Field(default_factory=list)


async def _claude_measures(payload: dict) -> dict:
    res = await llm_client.complete(
        feature="loss_advisor", system=_ADVISOR_SYSTEM,
        user=json.dumps(payload, default=str), schema=AdvisorOut,
        timeout_s=SWEEP_TIMEOUT_S, max_tokens=4000)
    if not res.ok:
        logger.warning("loss-review LLM failed: %s", res.error)
        return {"diagnosis": "LLM analysis unavailable — see aggregates.",
                "market_context": "", "measures": [], "_llm_failed": True}
    out = res.data.model_dump()
    # Unset params are dropped so the sanitiser sees the same shape as before
    # (an explicit null symbol and an absent one both mean "all symbols").
    for m in out["measures"]:
        m["params"] = {k: v for k, v in m["params"].items() if v is not None}
    return out


# ---------------------------------------------------------- auto-learning
def _qualifies(m: dict) -> bool:
    """Strict evidence bar for auto-apply."""
    ev = m.get("evidence") or {}
    if not ev.get("testable"):
        return False
    net = float(ev.get("net_effect") or 0)
    saved = float(ev.get("losses_avoided") or 0)
    missed = float(ev.get("wins_missed") or 0)
    total = ev.get("trades_total")
    if total and float(ev.get("trades_blocked") or 0) > AUTO_APPLY_MAX_BLOCK_SHARE * float(total):
        return False
    return net >= AUTO_APPLY_MIN_NET and saved >= AUTO_APPLY_RATIO * missed


def live_guard_block(signal: dict, guards: list) -> str | None:
    """Evaluate active auto-guards against a LIVE signal using the exact
    predicates that were shadow-tested. Returns a block reason or None."""
    pseudo_trade = {"symbol": signal.get("symbol"), "action": signal.get("action")}
    for g in guards:
        m = g.get("measure") or {}
        pred = _predicate(m)
        if pred is None:
            continue
        try:
            if pred(pseudo_trade, signal):
                net = ((g.get("latest_evidence") or g.get("evidence")) or {}).get("net_effect", 0)
                return (f"Auto-guard: {m.get('title') or m.get('type')} "
                        f"(shadow-tested net ${net:+.0f}/{SHADOW_DAYS}d)")
        except Exception:  # noqa: BLE001
            continue
    return None


async def _auto_apply_enabled(db, user_id: str) -> bool:
    try:
        u = await db.users.find_one({"_id": ObjectId(user_id)})
    except Exception:
        u = None
    s = (u or {}).get("postmortem_settings") or {}
    return bool(s.get("auto_apply_guards", True))


async def _auto_apply_suspended(db, user_id: str) -> str | None:
    """Audit v5 P0-4 — policy learning must never act while position truth
    is stale or executions are unreconciled. Fail CLOSED."""
    try:
        from state_contract import contract
        sc = await contract(db, user_id)
        if any(r["position_truth"] != "FRESH"
               for r in sc["accounts"] if r.get("bot_enabled")):
            return "position_truth_stale"
        ids = [r["account_id"] for r in sc["accounts"]]
        if ids and await db.execution_intents.count_documents(
                {"status": "unknown", "account_id": {"$in": ids}}):
            return "reconciliation_pending"
    except Exception:  # noqa: BLE001 — unknown truth = no policy changes
        return "truth_unavailable"
    return None


async def _auto_apply(db, user_id: str, review_doc: dict) -> list:
    """Apply qualifying measures: config knobs directly, gate types as
    live auto-guards. Everything logged, notified and reversible."""
    if not await _auto_apply_enabled(db, user_id):
        return []
    hold = await _auto_apply_suspended(db, user_id)
    if hold:
        review_doc["auto_apply_suspended"] = hold
        logger.warning("Auto-apply SUSPENDED user=%s reason=%s",
                       user_id, hold)
        return []
    applied = []
    active_n = await db.auto_guards.count_documents({"user_id": user_id, "active": True})
    now_iso = datetime.now(timezone.utc).isoformat()
    for m in review_doc.get("measures") or []:
        if not _qualifies(m):
            continue
        mtype = m.get("type")
        if mtype == "friday_flat":
            p = m.get("params") or {}
            upd = {"friday_flat_enabled": True}
            if p.get("mode") in ("close", "tighten"):
                upd["friday_flat_mode"] = p["mode"]
            try:
                mins = int(p.get("minutes_before") or 0)
                if 5 <= mins <= 480:
                    upd["friday_flat_minutes_before"] = mins
            except (TypeError, ValueError):
                pass
            r = await db.bot_configs.update_many({"user_id": user_id}, {"$set": upd})
            applied.append({"kind": "config", "measure": m,
                            "updated_configs": r.modified_count, "applied_at": now_iso})
            try:
                from change_governance import record_auto_applied
                await record_auto_applied(
                    db, user_id, "friday_flat_enabled", None, upd,
                    source="loss_advisor",
                    evidence=m.get("evidence"),
                    detail=m.get("title"))
            except Exception as e:  # noqa: BLE001
                logger.warning("governance ledger record failed "
                               "(measure still applied): %s", e)
        elif mtype in GATE_TYPES:
            if active_n >= MAX_ACTIVE_GUARDS:
                continue
            dup = await db.auto_guards.find_one({
                "user_id": user_id, "active": True,
                "measure.type": mtype, "measure.params": m.get("params") or {},
            })
            if dup:
                continue
            await db.auto_guards.insert_one({
                "user_id": user_id,
                "measure": {k: m[k] for k in
                            ("title", "type", "params", "rationale", "priority") if k in m},
                "evidence": m.get("evidence"),
                "review_id": str(review_doc.get("_id") or ""),
                "created_at": now_iso,
                "active": True,
                "source": "auto_learn",
            })
            active_n += 1
            applied.append({"kind": "guard", "measure": m, "applied_at": now_iso})
            try:
                from change_governance import record_auto_applied
                await record_auto_applied(
                    db, user_id, f"auto_guard:{mtype}", None,
                    m.get("params") or {}, source="loss_advisor",
                    evidence=m.get("evidence"), detail=m.get("title"))
            except Exception as e:  # noqa: BLE001
                logger.warning("governance ledger record failed "
                               "(guard still applied): %s", e)
    if applied:
        logger.warning("Auto-learning applied %d measure(s) user=%s", len(applied), user_id)
        try:
            from notifier import notify_auto_guard
            await notify_auto_guard(user_id, "applied", applied)
        except Exception as e:  # noqa: BLE001
            logger.debug("notify_auto_guard failed: %s", e)
    return applied


async def _revalidate_guards(db, user_id: str, ds: dict) -> list:
    """Re-shadow-test active guards on fresh data; auto-revert any guard
    that no longer meets the DECLARED gate (audit v5 P0-4): net effect
    ≥ $AUTO_APPLY_MIN_NET and losses avoided ≥ ratio × wins missed."""
    reverted = []
    async for g in db.auto_guards.find({"user_id": user_id, "active": True}):
        ev = _shadow_test(g.get("measure") or {}, ds)
        if ev is None:
            continue
        await db.auto_guards.update_one({"_id": g["_id"]}, {"$set": {"latest_evidence": ev}})
        net = float(ev.get("net_effect") or 0)
        saved = float(ev.get("losses_avoided") or 0)
        missed = float(ev.get("wins_missed") or 0)
        below_gate = (net < AUTO_APPLY_MIN_NET
                      or saved < AUTO_APPLY_RATIO * missed)
        if below_gate:
            reason = ("evidence_turned_negative" if net < 0
                      else "evidence_below_declared_gate")
            await db.auto_guards.update_one({"_id": g["_id"]}, {"$set": {
                "active": False,
                "reverted_at": datetime.now(timezone.utc).isoformat(),
                "revert_reason": reason,
            }})
            reverted.append({"measure": g.get("measure"), "evidence": ev,
                             "reason": reason})
            logger.warning("Auto-guard reverted (%s) user=%s: %s",
                           reason, user_id,
                           (g.get("measure") or {}).get("title"))
    if reverted:
        try:
            from notifier import notify_auto_guard
            await notify_auto_guard(user_id, "auto_reverted", reverted)
        except Exception as e:  # noqa: BLE001
            logger.debug("notify_auto_guard failed: %s", e)
    return reverted


# --------------------------------------------------------------- main runs
async def run_loss_review(db, user_id: str, trigger: str = "auto") -> dict | None:
    ds = await _gather(db, user_id)
    if not ds["losses"]:
        return None
    agg = _aggregates(ds)

    # Current market snapshot — best effort (same sources as post-mortems).
    market_now = {}
    try:
        from macro_feeds import get_macro_snapshot
        market_now["macro"] = (await get_macro_snapshot() or {}).get("summary")
    except Exception:
        pass
    try:
        latest_sig = await db.signals.find_one({"user_id": user_id}, sort=[("_id", -1)])
        if latest_sig:
            reg = latest_sig.get("regime")
            market_now["regime"] = reg.get("regime") if isinstance(reg, dict) else reg
            kf = ((latest_sig.get("indicators") or {}).get("kalman_filter")
                  or latest_sig.get("kalman_filter") or {})
            market_now["kalman_velocity"] = kf.get("k_velocity")
            market_now["mtf_htf_trend"] = (latest_sig.get("mtf_gate") or {}).get("htf_trend")
    except Exception:
        pass

    # Per-loss post-mortem clusters give the LLM richer context.
    patterns: dict = defaultdict(int)
    async for pm in db.loss_postmortems.find(
            {"user_id": user_id, "created_at": {"$gte": ds["since_loss"]}}):
        patterns[pm.get("pattern_key") or "?"] += 1

    verdict = await _claude_measures({
        "aggregates": agg,
        "postmortem_pattern_counts": dict(patterns),
        "market_now": market_now,
    })

    measures = []
    raw_measures = verdict.get("measures")
    if not isinstance(raw_measures, list):
        raw_measures = []
    for m in raw_measures[:5]:
        m = _sanitize_measure(m)
        if m is None:
            continue
        m["evidence"] = _shadow_test(m, ds)
        measures.append(m)
    # Evidence-backed ranking: best net effect first, untestable last.
    measures.sort(key=lambda m: -((m.get("evidence") or {}).get("net_effect")
                                  if m.get("evidence") else -1e9))

    doc = {
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "aggregates": agg,
        "market_now": market_now,
        "diagnosis": verdict.get("diagnosis"),
        "market_context": verdict.get("market_context"),
        "measures": measures,
        "llm_failed": bool(verdict.get("_llm_failed")),
    }
    res = await db.loss_reviews.insert_one(doc)
    doc["_id"] = res.inserted_id
    logger.info("Loss review stored user=%s losses=%d measures=%d",
                user_id, agg["losses"], len(measures))

    # iter-55 · Daily Auto-Learning: re-validate existing guards on fresh
    # data (auto-revert net-negative ones), then auto-apply new qualifying
    # measures. Both logged on the review + Telegram.
    doc["auto_reverted"] = await _revalidate_guards(db, user_id, ds)
    doc["auto_applied"] = await _auto_apply(db, user_id, doc)
    if doc["auto_applied"] or doc["auto_reverted"]:
        await db.loss_reviews.update_one({"_id": res.inserted_id}, {"$set": {
            "auto_applied": doc["auto_applied"],
            "auto_reverted": doc["auto_reverted"],
        }})

    await ws_manager.broadcast(user_id, "loss_review", {
        "review_id": str(res.inserted_id),
        "losses": agg["losses"],
        "loss_pnl": agg["loss_pnl"],
        "measures": len(measures),
    })
    try:
        from notifier import notify_loss_review
        await notify_loss_review(user_id, doc)
    except Exception as e:  # noqa: BLE001
        logger.debug("notify_loss_review failed: %s", e)
    return doc


async def _due_for_auto_review(db, user_id: str) -> bool:
    last = await db.loss_reviews.find_one({"user_id": user_id}, sort=[("created_at", -1)])
    since = None
    if last:
        try:
            last_at = datetime.fromisoformat(last["created_at"])
            if (datetime.now(timezone.utc) - last_at).total_seconds() < COOLDOWN_HOURS * 3600:
                return False
            since = last["created_at"]
        except (ValueError, KeyError):
            pass
    new_losses = await db.trades.count_documents({
        "user_id": user_id, "status": "closed", "origin": "auto",
        "pnl": {"$lt": 0},
        "pnl_estimated": {"$ne": True}, "pnl_unknown": {"$ne": True},
        "closed_at": {"$gte": since or (datetime.now(timezone.utc)
                                        - timedelta(days=WINDOW_DAYS)).isoformat()},
    })
    return new_losses >= MIN_NEW_LOSSES


_last_sweep_check: datetime | None = None


async def sweep_loss_reviews(db) -> int:
    """Called from the bot loop. Cheap gating, throttled to one check/10 min."""
    global _last_sweep_check
    now = datetime.now(timezone.utc)
    if _last_sweep_check and (now - _last_sweep_check).total_seconds() < 600:
        return 0
    _last_sweep_check = now
    users = await db.bot_configs.distinct("user_id", {"active": True})
    produced = 0
    for uid in users:
        try:
            if await _due_for_auto_review(db, uid):
                if await run_loss_review(db, uid, trigger="auto"):
                    produced += 1
        except Exception as e:  # noqa: BLE001
            logger.exception("loss review failed user=%s: %s", uid, e)
    return produced


__all__ = ["run_loss_review", "sweep_loss_reviews", "live_guard_block"]
