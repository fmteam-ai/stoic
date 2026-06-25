"""Trade Explainer — Explainable AI for every executed trade.

Composes a 4-section explanation from the agent_activity log that was
written when the orchestrator analysed the tick. Pulls the most recent
agent_activity row for (user_id, symbol) within the trade window and
distills it into:

  1. WHY DID I ENTER?       — Strategy bias, technical setup, sentiment
  2. WHY THIS SIZE?         — original lot, Allocator trim factors, Risk vetoes
  3. WHAT FACTORS MATTERED? — top 3 ranked contributors with magnitudes
  4. WHAT RISKS EXIST?      — open SL distance, macro gate, drawdown room

The explanation is read-only — pure read from existing collections, no
writes. We store a snapshot on the trade doc at creation time
(`trade.explanation_snapshot`) so it survives even after activity rows
are pruned.
"""
from datetime import datetime, timezone, timedelta
import logging

logger = logging.getLogger("trade.explainer")


def _step(activity: dict, name: str) -> dict:
    for s in (activity or {}).get("steps") or []:
        if s.get("agent") == name:
            return s
    return {}


async def _latest_activity(db, *, user_id: str, symbol: str, near_ts: str | None = None):
    q = {"user_id": user_id, "symbol": symbol.upper()}
    if near_ts:
        try:
            ts = datetime.fromisoformat(str(near_ts).replace("Z", "+00:00"))
            q["completed_at"] = {
                "$gte": (ts - timedelta(minutes=5)).isoformat(),
                "$lte": (ts + timedelta(minutes=5)).isoformat(),
            }
        except Exception:
            pass
    return await db.agent_activity.find_one(q, sort=[("completed_at", -1)])


def _explain_entry(activity: dict, signal: dict | None) -> dict:
    strat = _step(activity, "strategy")
    tech  = _step(activity, "technical")
    macro = _step(activity, "macro")
    news  = _step(activity, "news_sentiment")
    return {
        "action":     (signal or {}).get("action") or activity.get("final_action"),
        "confidence": (signal or {}).get("confidence") or activity.get("final_confidence"),
        "strategy_summary": strat.get("summary"),
        "technical_bias":   tech.get("summary"),
        "macro_bias":       macro.get("summary"),
        "news_bias":        news.get("summary"),
    }


def _explain_sizing(activity: dict, trade: dict) -> dict:
    alloc = _step(activity, "portfolio_allocator")
    details = alloc.get("details") or {}
    return {
        "final_lot": trade.get("lot_size"),
        "original_lot": details.get("original_lot"),
        "vol_parity_scale": details.get("vol_parity_scale"),
        "kelly_scale": details.get("kelly_scale"),
        "blended_scale": details.get("blended_scale"),
        "win_rate_30d": details.get("win_rate"),
        "atr_pct": details.get("atr_pct"),
        "applied": alloc.get("status") == "ok",
        "reason": (alloc.get("details") or {}).get("reason") or alloc.get("summary"),
    }


def _explain_factors(activity: dict) -> list[dict]:
    """Rank the agents that contributed strongest signals.

    Heuristic ranking — we have qualitative bias strings, not numeric weights,
    so we rank by 'how decisive each agent was' using simple heuristics:
      - News sentiment with abs(score) ≥ 0.3 → high impact
      - Macro freeze / blocked gate → high impact
      - Strategy confidence ≥ 75 → high impact
      - Technical RSI extreme → medium impact
    """
    factors: list[dict] = []
    for s in (activity or {}).get("steps") or []:
        agent = s.get("agent")
        summ = s.get("summary") or ""
        if agent == "strategy" and s.get("status") == "ok":
            factors.append({
                "agent": "strategy", "label": "Strategy Confidence",
                "impact": "HIGH" if "%" in summ and any(c.isdigit() for c in summ) else "MEDIUM",
                "detail": summ,
            })
        elif agent == "macro" and ("FREEZE" in summ.upper() or "BLOCK" in summ.upper()):
            factors.append({"agent": "macro", "label": "Macro Regime",
                            "impact": "HIGH", "detail": summ})
        elif agent == "news_sentiment" and any(t in summ for t in ("BULLISH", "BEARISH")):
            factors.append({"agent": "news_sentiment", "label": "News Sentiment",
                            "impact": "MEDIUM", "detail": summ})
        elif agent == "technical" and ("OVERBOUGHT" in summ or "OVERSOLD" in summ):
            factors.append({"agent": "technical", "label": "Technical Extreme",
                            "impact": "MEDIUM", "detail": summ})
        elif agent == "risk" and s.get("status") == "vetoed":
            factors.append({"agent": "risk", "label": "Risk Veto",
                            "impact": "HIGH", "detail": summ})
    # Cap at top 3 — explanations should be punchy, not exhaustive.
    return factors[:3]


def _explain_risks(trade: dict, activity: dict) -> dict:
    safety = (trade or {}).get("safety_audit") or {}
    macro = _step(activity, "macro")
    macro_gate = (macro.get("details") or {}).get("macro_gate") if isinstance(macro.get("details"), dict) else None
    risk_step = _step(activity, "risk")
    return {
        "stop_loss":   trade.get("stop_loss"),
        "take_profit": trade.get("take_profit"),
        "sl_pips_risk": safety.get("sl_distance_pips"),
        "max_loss_usd": safety.get("max_loss_usd"),
        "risk_pct_of_equity": safety.get("risk_pct_of_equity"),
        "macro_gate_open": (macro_gate or {}).get("buy_ok") if macro_gate else None,
        "risk_overrides": risk_step.get("overrides") or [],
    }


async def explain_trade(db, trade: dict) -> dict:
    """Build the full 4-section explanation for one trade.

    Prefers the snapshot stored on the trade itself (preserves history even
    after agent_activity is pruned); falls back to live composition.
    """
    snap = trade.get("explanation_snapshot")
    if snap:
        return snap

    activity = await _latest_activity(
        db, user_id=trade["user_id"], symbol=trade["symbol"],
        near_ts=trade.get("opened_at"),
    ) or {}
    signal_like = {"action": trade.get("action"),
                   "confidence": trade.get("confidence")}
    return {
        "trade_id": str(trade.get("_id") or trade.get("id") or ""),
        "symbol": trade.get("symbol"),
        "opened_at": trade.get("opened_at"),
        "tick_id": activity.get("tick_id"),
        "entry":    _explain_entry(activity, signal_like),
        "sizing":   _explain_sizing(activity, trade),
        "factors":  _explain_factors(activity),
        "risks":    _explain_risks(trade, activity),
        "live": bool(activity),
    }


async def snapshot_for_trade_creation(db, *, user_id: str, symbol: str,
                                       signal: dict, trade_doc: dict) -> dict:
    """Build the snapshot to embed on a fresh trade doc before insert.

    Called from execution engines. The snapshot pins the explanation at
    creation time so it stays stable even if agent_activity rotates.
    """
    activity = await _latest_activity(db, user_id=user_id, symbol=symbol) or {}
    return {
        "tick_id": activity.get("tick_id"),
        "entry":    _explain_entry(activity, signal),
        "sizing":   _explain_sizing(activity, trade_doc),
        "factors":  _explain_factors(activity),
        "risks":    _explain_risks(trade_doc, activity),
        "snapshotted_at": datetime.now(timezone.utc).isoformat(),
    }
