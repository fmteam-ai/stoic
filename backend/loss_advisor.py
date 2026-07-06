"""Auto Loss Review — iter-54.

Aggregate, automatic analysis of recent losing trades. On top of the per-loss
post-mortems (loss_postmortem.py), this module periodically reviews ALL exact
losses of the last window together with current market state, asks Claude for
concrete counter-measures from a FIXED menu, then SHADOW-TESTS each measure
against the user's actual recent trades:

    "would have avoided $X of losses / missed $Y of wins → net $Z"

so every suggestion ships with hard evidence (today's lesson: a plausible
guardrail can silently kill a winning strategy). Measures are NEVER
auto-applied — they are advisory, ranked, and delivered via Loss Lab + Telegram.

Collections:
  • `loss_reviews` — one doc per review run (auto or manual)
"""
import os
import json
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from bson import ObjectId

from pip_utils import base_symbol
from regime_adapter import velocity_veto
from ws_manager import manager as ws_manager
from emergentintegrations.llm.chat import LlmChat, UserMessage

logger = logging.getLogger("loss-advisor")

WINDOW_DAYS = 7          # losses analysed
SHADOW_DAYS = 14         # trades replayed for evidence
MIN_NEW_LOSSES = 3       # auto-run gate
COOLDOWN_HOURS = 24      # min gap between auto reviews per user

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
        "losses_avoided": round(losses_avoided, 2),
        "wins_missed": round(wins_missed, 2),
        "net_effect": round(losses_avoided - wins_missed, 2),
    }


# -------------------------------------------------------------------- LLM
async def _claude_measures(payload: dict) -> dict:
    try:
        chat = LlmChat(
            api_key=os.environ["EMERGENT_LLM_KEY"],
            session_id=f"loss-review-{uuid.uuid4().hex[:8]}",
            system_message=_ADVISOR_SYSTEM,
        ).with_model("anthropic", "claude-sonnet-4-5-20250929")
        raw = str(await chat.send_message(UserMessage(text=json.dumps(payload, default=str)))).strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        out = json.loads(raw)
        out.setdefault("measures", [])
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("loss-review LLM failed: %s", e)
        return {"diagnosis": "LLM analysis unavailable — see aggregates.",
                "market_context": "", "measures": [], "_llm_failed": True}


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
    for m in (verdict.get("measures") or [])[:5]:
        m = dict(m)
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


__all__ = ["run_loss_review", "sweep_loss_reviews"]
