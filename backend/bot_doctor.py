"""Bot Doctor — STOIC's self-diagnosis subsystem (iter-75 · LITE).

Reads the last hour of operational telemetry (trade failures, bot pulses,
veto stats, account heartbeats, blocked accounts) and asks Claude
Sonnet 4.5 to produce a structured diagnosis.

This is the LITE version: NO auto-apply. The diagnosis surfaces on the
dashboard as a tile with human-readable findings + ranked remediation
recommendations. The user stays in the loop on every fix.

Public surface:
    diagnose(db, user_id, account_id=None) -> {
        "status": "healthy" | "watch" | "degraded" | "critical",
        "headline": str,                # one-liner
        "findings": [str, ...],         # 2-5 bullet observations
        "root_cause_hypothesis": str,   # best guess, with confidence %
        "recommendations": [            # ranked, no auto-apply
            {"action": str, "rationale": str, "effort": "low"|"medium"|"high",
             "destructive": bool}
        ],
        "evidence": {...},              # snapshots of the data used
        "generated_at": ISO,
        "cache_ttl_seconds": int,
        "_llm_failed": bool,
    }

Cached for 5 minutes per (user_id, account_id) to keep LLM cost bounded.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional


from pydantic import BaseModel, Field, field_validator

import llm_client
from database import get_db

logger = logging.getLogger("bot_doctor")

CACHE_TTL_SECONDS = 300  # 5 minutes
LOOKBACK_MINUTES = 60

_DOCTOR_SYSTEM = """You are STOIC Bot Doctor — a senior algorithmic-trading
SRE who diagnoses what's wrong with a running AI trading bot.

You will be given a JSON snapshot of the last hour of operational telemetry:
recent trade failures, bot pulses, broker-reject events, account heartbeats,
veto counts, regime state, and any blocked accounts.

Return STRICTLY valid JSON in this exact shape (no markdown, no prose):

{
  "status": "healthy" | "watch" | "degraded" | "critical",
  "headline": "<one sentence — what's the situation?>",
  "findings": ["<observation 1>", "<observation 2>", ...],
  "root_cause_hypothesis": "<best guess + confidence%, e.g. 'Symbol suffix mismatch on Tauro (87%)'>",
  "recommendations": [
    {"action": "<concrete action>", "rationale": "<why>",
     "effort": "low"|"medium"|"high", "destructive": false}
  ],
  "evidence": [{"key": "<key>", "value": "<value pulled from telemetry>"}]
}

RULES:
- 2 to 5 findings (skip if telemetry is healthy)
- 1 to 3 recommendations, ranked best-first
- Be concrete: never say "check logs" — name the file, the retcode, the symbol
- Mark `destructive: true` ONLY for actions that close trades, change risk, or halt accounts
- Status guidance:
    healthy   = no anomalies, bot is patiently filtering
    watch     = minor pattern worth flagging (e.g. wider spreads today)
    degraded  = active problem reducing performance (e.g. 1 account halted)
    critical  = capital at risk, immediate human attention needed
- If you cannot diagnose with confidence, return status="watch" and say so
- Stay focused on the SINGLE biggest issue — don't list every minor thing
"""


async def _collect_telemetry(db, user_id: str,
                             account_id: Optional[str] = None) -> dict:
    """Pull the last hour of operational data needed for diagnosis."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(minutes=LOOKBACK_MINUTES)).isoformat()

    # 1. Failed trades
    trade_q = {"user_id": user_id, "status": "failed",
               "opened_at": {"$gte": since}}
    if account_id:
        trade_q["account_id"] = account_id
    failed_cursor = db.trades.find(trade_q, projection={
        "symbol": 1, "base_symbol": 1, "symbol_suffix_applied": 1,
        "action": 1, "error": 1, "broker": 1, "opened_at": 1,
        "account_id": 1,
    }).sort("opened_at", -1).limit(15)
    failed = [t async for t in failed_cursor]

    # 2. Closed trades (for win-rate context)
    closed_q = {"user_id": user_id, "status": "closed", "origin": "auto",
                "closed_at": {"$gte": since}}
    if account_id:
        closed_q["account_id"] = account_id
    closed_cursor = db.trades.find(closed_q, projection={
        "symbol": 1, "pnl": 1, "close_reason": 1, "closed_at": 1,
        "account_id": 1, "origin": 1,
    }).sort("closed_at", -1).limit(20)
    closed = [t async for t in closed_cursor]
    wins = sum(1 for t in closed if (t.get("pnl") or 0) > 0)
    losses = sum(1 for t in closed if (t.get("pnl") or 0) < 0)

    # 3. Accounts (blocked status + heartbeat health)
    acct_q = {"user_id": user_id}
    if account_id:
        acct_q["_id"] = account_id
    acct_cursor = db.accounts.find(acct_q, projection={
        "label": 1, "broker": 1, "status": 1, "ea_version": 1,
        "trading_blocked": 1, "block_reason": 1, "block_retcode_label": 1,
        "symbol_suffix": 1, "last_heartbeat": 1, "mode": 1,
    })
    accounts = []
    for a in [a async for a in acct_cursor]:
        hb = a.get("last_heartbeat")
        hb_age = None
        if hb:
            try:
                hb_age = int((now - datetime.fromisoformat(
                    hb.replace("Z", "+00:00"))).total_seconds())
            except Exception:
                pass
        accounts.append({
            "label": a.get("label"),
            "broker": a.get("broker"),
            "status": a.get("status"),
            "ea_version": a.get("ea_version"),
            "trading_blocked": bool(a.get("trading_blocked")),
            "block_reason": a.get("block_reason"),
            "block_retcode_label": a.get("block_retcode_label"),
            "symbol_suffix": a.get("symbol_suffix"),
            "heartbeat_age_seconds": hb_age,
            "mode": a.get("mode"),
        })

    # 4. Recent signals (action distribution + veto patterns)
    sig_q = {"user_id": user_id, "created_at": {"$gte": since}}
    if account_id:
        sig_q["account_id"] = account_id
    sig_cursor = db.signals.find(sig_q, projection={
        "action": 1, "confidence": 1, "reasoning": 1,
        "regime_execution_mode": 1, "veto_summary": 1,
    }).sort("created_at", -1).limit(40)
    signals = [s async for s in sig_cursor]
    action_counts: dict[str, int] = {}
    veto_counts: dict[str, int] = {}
    for s in signals:
        a = s.get("action", "?")
        action_counts[a] = action_counts.get(a, 0) + 1
        vs = s.get("veto_summary") or {}
        if isinstance(vs, dict):
            for k in vs:
                veto_counts[k] = veto_counts.get(k, 0) + 1

    last_signal_reasoning = None
    if signals:
        last_signal_reasoning = (signals[0].get("reasoning") or "")[:300]
        last_regime = ((signals[0].get("regime_execution_mode") or {})
                       .get("execution_mode"))
    else:
        last_regime = None

    return {
        "now_iso": now.isoformat(),
        "lookback_minutes": LOOKBACK_MINUTES,
        "failed_trades": [{
            "opened_at": (t.get("opened_at") or "")[:19],
            "broker": t.get("broker"),
            "symbol": t.get("symbol"),
            "suffix_applied": t.get("symbol_suffix_applied"),
            "error": (t.get("error") or "")[:120],
        } for t in failed[:10]],
        "failed_trade_count": len(failed),
        "closed_trade_stats": {
            "count": len(closed),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(100.0 * wins / (wins + losses), 1) if (wins + losses) else None,
            "net_pnl": round(sum(t.get("pnl") or 0 for t in closed), 2),
        },
        "accounts": accounts,
        "signal_stats": {
            "total": len(signals),
            "actions": action_counts,
            "vetos": veto_counts,
            "last_regime_execution_mode": last_regime,
            "last_signal_reasoning_snippet": last_signal_reasoning,
        },
    }


async def diagnose(db=None, user_id: str = None,
                   account_id: Optional[str] = None,
                   force_refresh: bool = False) -> dict:
    """Public entrypoint. Returns the cached or fresh diagnosis."""
    if db is None:
        db = get_db()
    if not user_id:
        raise ValueError("diagnose: user_id is required")

    cache_key = f"{user_id}:{account_id or 'all'}"
    cache_collection = db.bot_doctor_cache

    # Cache lookup (5-min TTL)
    if not force_refresh:
        cached = await cache_collection.find_one({"_id": cache_key})
        if cached:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(cached["generated_at"]
                                            .replace("Z", "+00:00"))
                   ).total_seconds()
            if age < CACHE_TTL_SECONDS:
                return {**cached["diagnosis"],
                        "cache_age_seconds": int(age),
                        "cache_hit": True}

    telemetry = await _collect_telemetry(db, user_id, account_id)
    diagnosis = await _ask_llm(telemetry)

    diagnosis["generated_at"] = datetime.now(timezone.utc).isoformat()
    diagnosis["cache_ttl_seconds"] = CACHE_TTL_SECONDS
    diagnosis["evidence"] = diagnosis.get("evidence") or {}
    diagnosis["evidence"]["_telemetry_snapshot"] = telemetry

    # Persist for next 5 min
    await cache_collection.replace_one(
        {"_id": cache_key},
        {"_id": cache_key,
         "user_id": user_id, "account_id": account_id,
         "generated_at": diagnosis["generated_at"],
         "diagnosis": diagnosis},
        upsert=True,
    )

    return {**diagnosis, "cache_age_seconds": 0, "cache_hit": False}


class _DoctorRec(BaseModel):
    action: str
    rationale: str = ""
    effort: str = "medium"
    destructive: bool = False


class _EvidenceItem(BaseModel):
    key: str
    value: str

    @field_validator("key", "value", mode="before")
    @classmethod
    def _to_str(cls, v):
        return "" if v is None else str(v)


class DoctorOut(BaseModel):
    status: str
    headline: str = ""
    findings: list[str] = Field(default_factory=list)
    root_cause_hypothesis: str = ""
    recommendations: list[_DoctorRec] = Field(default_factory=list)
    # Structured outputs need fixed object keys, so the free-form evidence
    # map travels as key/value pairs and is folded back into a dict.
    evidence: list[_EvidenceItem] = Field(default_factory=list)


async def _ask_llm(telemetry: dict) -> dict:
    """LLM call → strict JSON response. Falls back to a deterministic
    rule-based diagnosis when the LLM is unavailable so the dashboard
    tile is never empty."""
    user_text = (
        "Diagnose the bot. Telemetry snapshot from the last "
        f"{LOOKBACK_MINUTES} minutes follows. Return JSON only.\n\n"
        + json.dumps(telemetry, default=str)
    )
    res = await llm_client.complete(
        feature="bot_doctor", system=_DOCTOR_SYSTEM, user=user_text,
        schema=DoctorOut, max_tokens=2000)
    if not res.ok:
        logger.warning("Bot Doctor LLM failed: %s — using rule-based fallback", res.error)
        return _rule_based_fallback(telemetry, llm_error=str(res.error))
    out = res.data.model_dump()
    out["evidence"] = {e.key: e.value for e in res.data.evidence}
    return out


def _rule_based_fallback(t: dict, llm_error: str = "") -> dict:
    """Deterministic, no-LLM diagnosis. Less nuanced but always available.
    Triggered when EMERGENT_LLM_KEY is missing OR the API call fails."""
    accts = t.get("accounts", [])
    blocked = [a for a in accts if a.get("trading_blocked")]
    stale = [a for a in accts
             if (a.get("heartbeat_age_seconds") or 0) > 300]
    failed_count = t.get("failed_trade_count", 0)
    sig_stats = t.get("signal_stats", {})
    closed_stats = t.get("closed_trade_stats", {})

    findings: list[str] = []
    recommendations: list[dict] = []
    status = "healthy"
    headline = "All systems nominal — bot is patiently filtering signals."

    if blocked:
        status = "critical"
        b = blocked[0]
        headline = f"Account {b.get('label')} auto-halted: {b.get('block_retcode_label') or 'broker reject'}"
        findings.append(
            f"{len(blocked)} account(s) halted by broker-reject breaker: "
            + ", ".join(f"{a.get('label')} ({a.get('block_retcode_label')})"
                        for a in blocked)
        )
        recommendations.append({
            "action": f"Check Symbols in {b.get('label')} MT5 MarketWatch (Ctrl+U), then set symbol_suffix via Accounts page",
            "rationale": "Broker-side rejects almost always mean symbol-name mismatch or missing instrument",
            "effort": "low",
            "destructive": False,
        })
    elif stale:
        status = "degraded"
        headline = f"{len(stale)} account(s) have stale heartbeats (>5min)"
        findings.append(
            "Stale EAs: " + ", ".join(
                f"{a.get('label')} ({a.get('heartbeat_age_seconds')}s)"
                for a in stale)
        )
        recommendations.append({
            "action": "Re-attach the EA to its chart in MT5; verify AlgoTrading is enabled",
            "rationale": "Bot can't trade through an EA that isn't phoning home",
            "effort": "low",
            "destructive": False,
        })
    elif failed_count >= 3:
        status = "degraded"
        headline = f"{failed_count} trade failures in the last hour"
        findings.append(f"{failed_count} failed trades — review most recent errors")
        recommendations.append({
            "action": "Open Trades page → filter by status=failed; check the `error` column for a recurring retcode",
            "rationale": "Persistent failures usually point at a single broker-side root cause",
            "effort": "low",
            "destructive": False,
        })

    if closed_stats.get("win_rate_pct") is not None and closed_stats["win_rate_pct"] < 40 \
            and closed_stats.get("count", 0) >= 5:
        if status == "healthy":
            status = "watch"
            headline = f"Win rate {closed_stats['win_rate_pct']}% in last hour — below comfort floor"
        findings.append(
            f"Win rate {closed_stats['win_rate_pct']}% across "
            f"{closed_stats['count']} closed trades")
        recommendations.append({
            "action": "Enable adaptive_risk on the affected account (auto-cuts size after losing streaks)",
            "rationale": "Cap drawdown without halting the bot entirely",
            "effort": "low",
            "destructive": False,
        })

    if sig_stats.get("total", 0) == 0:
        if status == "healthy":
            status = "watch"
            headline = "No signals in the last hour — bot may be deeply in HOLD"
        findings.append("Zero signals emitted — check market hours + entropy filter")

    if not findings:
        findings.append(f"{closed_stats.get('count', 0)} closed trades, "
                        f"{sig_stats.get('total', 0)} signals analyzed, "
                        f"{len([a for a in accts if not a.get('trading_blocked')])} accounts healthy")

    return {
        "status": status,
        "headline": headline,
        "findings": findings,
        "root_cause_hypothesis": "Rule-based analysis (LLM unavailable)" if llm_error else "Rule-based — no LLM",
        "recommendations": recommendations or [{
            "action": "Continue observation; no remediation needed",
            "rationale": "Bot is operating within healthy parameters",
            "effort": "low",
            "destructive": False,
        }],
        "evidence": {"accounts_blocked": len(blocked),
                     "accounts_stale": len(stale),
                     "failed_trades_last_hour": failed_count,
                     "win_rate_last_hour": closed_stats.get("win_rate_pct")},
        "_llm_failed": bool(llm_error),
    }
