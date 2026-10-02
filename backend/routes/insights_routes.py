"""Insights aggregation routes — Weekly AI Digest and related analytical
summaries the dashboard surfaces to users.
"""
import os
import logging
import uuid
from datetime import datetime, timedelta, timezone
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from auth import get_current_user
from database import get_db
from email_sender import send_email, is_configured as email_is_configured
from llm_models import provider_model

logger = logging.getLogger("insights")
router = APIRouter(prefix="/insights", tags=["insights"])


_REFLECTION_SYSTEM_PROMPT = """You are STOIC's Weekly Reflection writer — a calm, pragmatic
trading coach. Given a JSON digest of the last 7 days, write a 3-paragraph
reflection in plain English (no markdown). 80-120 words per paragraph.

Paragraph 1 — What happened: trade count, win rate, net P&L, best/worst.
Paragraph 2 — Patterns: recurring HOLD reasons, auto-heal kinds, any session
   bias visible in the data. Acknowledge wins without bragging.
Paragraph 3 — Focus for next week: one specific, actionable adjustment
   (NEVER advise raising risk on a losing week). End with one sentence of
   stoic encouragement.

Tone: composed, factual, never alarmist. Refer to the user as "you".
Output: plain text only. No markdown, no JSON, no headings."""


async def _generate_ai_reflection(digest_payload: dict) -> Optional[str]:
    """Ask Claude Sonnet 4.5 for a narrative reflection on the digest data."""
    key = os.environ.get("EMERGENT_LLM_KEY")
    if not key:
        return None
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat = LlmChat(
            api_key=key,
            session_id=f"weekly-digest-{uuid.uuid4().hex[:8]}",
            system_message=_REFLECTION_SYSTEM_PROMPT,
        ).with_model(*provider_model("weekly_insights"))
        # Pass a slim payload — Claude doesn't need the raw trade list.
        slim = {
            "stats": digest_payload.get("stats"),
            "best_trade": digest_payload.get("best_trade"),
            "worst_trade": digest_payload.get("worst_trade"),
            "auto_heal_breakdown": digest_payload.get("auto_heal_breakdown"),
            "hold_reasons": digest_payload.get("hold_reasons"),
            "window_days": digest_payload.get("window_days"),
        }
        prompt = f"Here is this week's digest:\n\n{slim}\n\nWrite the reflection."
        response = await chat.send_message(UserMessage(text=prompt))
        text = str(response).strip()
        return text or None
    except Exception as e:
        logger.warning("weekly-digest reflection failed: %s", e)
        return None


def _safe_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _summarise_hold_reasons(reasonings: list[str]) -> list[dict]:
    """Bucket bot HOLD reasons into 4 canonical categories + count occurrences."""
    buckets = {
        "Noisy entropy": 0,
        "Market closed": 0,
        "Macro freeze": 0,
        "Regime CHOP": 0,
        "Other": 0,
    }
    for r in reasonings:
        rl = (r or "").lower()
        if not rl:
            continue
        if "market closed" in rl:
            buckets["Market closed"] += 1
        elif "noise" in rl or "entropy" in rl:
            buckets["Noisy entropy"] += 1
        elif "macro" in rl or "freeze" in rl:
            buckets["Macro freeze"] += 1
        elif "chop" in rl:
            buckets["Regime CHOP"] += 1
        else:
            buckets["Other"] += 1
    return [{"label": k, "count": v} for k, v in buckets.items() if v > 0]


def _suggest_action(stats: dict) -> str:
    """Rule-based suggestion line — friendly, actionable, never alarmist."""
    win_rate = stats["win_rate"]
    pnl = stats["pnl_total"]
    trades = stats["trades"]
    heals = stats["auto_heals"]

    if trades == 0:
        return ("No trades closed this week. Market entropy and weekend closures kept "
                "the bot patient — consider adding BTCUSD to widen the signal surface.")
    if pnl < 0 and win_rate < 35:
        return ("Win rate below 35% and net negative — Loss Lab is the next stop. "
                "Auto-heal will tighten guardrails automatically on the next cycle.")
    if pnl > 0 and win_rate >= 60:
        return ("Strong week — win rate ≥ 60% with positive P&L. Consider unlocking "
                "the next risk tier or increasing per-trade sizing slightly.")
    if heals >= 3:
        return ("Auto-heal triggered ≥3 times — the bot is actively retuning. "
                "Hold the current configuration for one more week before manual changes.")
    return ("Steady week — keep the current configuration. Add more symbols to the "
            "watchlist if you want higher trade frequency.")


@router.get("/weekly-digest")
async def weekly_digest(days: int = 7, include_ai: bool = False,
                        user=Depends(get_current_user)):
    """Aggregated 7-day (or N-day) recap of the user's bot activity.

    Powers the dashboard's Weekly AI Digest widget. Returns:
      - trade stats (count, win rate, P&L, best, worst)
      - auto-heal action breakdown by kind
      - top HOLD reasons during the window
      - a suggested next action (rule-based)
    """
    db = get_db()
    days = max(1, min(int(days or 7), 30))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since.isoformat()
    since_date_iso = since.date().isoformat()

    # ----- Trades closed in window -----
    trade_q = {
        "user_id": user["id"],
        "status": "closed",
        "closed_at": {"$gte": since_date_iso},
    }
    trades = await db.trades.find(trade_q).to_list(length=5000)
    n_trades = len(trades)
    wins = [t for t in trades if (t.get("pnl") or 0) > 0]
    losses = [t for t in trades if (t.get("pnl") or 0) < 0]
    n_wins, n_losses = len(wins), len(losses)
    win_rate = round((n_wins / n_trades * 100), 1) if n_trades else 0.0
    pnl_total = round(sum((t.get("pnl") or 0) for t in trades), 2)
    avg_win = round(sum((t.get("pnl") or 0) for t in wins) / n_wins, 2) if wins else 0.0
    avg_loss = round(sum((t.get("pnl") or 0) for t in losses) / n_losses, 2) if losses else 0.0

    def _trade_slim(t: dict) -> dict:
        return {
            "id": str(t.get("_id")),
            "symbol": t.get("symbol"),
            "action": t.get("action"),
            "pnl": round(float(t.get("pnl") or 0), 2),
            "closed_at": t.get("closed_at"),
        }

    best_trade = _trade_slim(max(trades, key=lambda x: x.get("pnl") or 0)) if trades else None
    worst_trade = _trade_slim(min(trades, key=lambda x: x.get("pnl") or 0)) if trades else None

    # ----- Auto-heal actions in window -----
    heal_q = {"user_id": user["id"], "ts": {"$gte": since_iso}}
    heals = await db.auto_heal_actions.find(heal_q).to_list(length=1000)
    heal_buckets: dict = {}
    for h in heals:
        k = h.get("kind") or "unknown"
        heal_buckets[k] = heal_buckets.get(k, 0) + 1
    heal_breakdown = [{"kind": k, "count": v} for k, v in
                      sorted(heal_buckets.items(), key=lambda x: -x[1])]

    # ----- HOLD reasons in window -----
    signal_q = {"action": "HOLD", "created_at": {"$gte": since_iso}}
    signals = await db.signals.find(signal_q).limit(2000).to_list(length=2000)
    hold_reasons = _summarise_hold_reasons([s.get("reasoning", "") for s in signals])

    stats = {
        "trades": n_trades,
        "wins": n_wins,
        "losses": n_losses,
        "win_rate": win_rate,
        "pnl_total": pnl_total,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "auto_heals": len(heals),
    }

    payload = {
        "window_days": days,
        "window_start": since_iso,
        "stats": stats,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "auto_heal_breakdown": heal_breakdown,
        "hold_reasons": hold_reasons,
        "suggested_action": _suggest_action(stats),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    # iter-69 · Optional LLM-written reflection (Claude Sonnet 4.5).
    # Frontend opts in via ?include_ai=true to avoid a slow LLM call on
    # every dashboard refresh.
    if include_ai:
        payload["ai_reflection"] = await _generate_ai_reflection(payload)
    return payload


def _render_digest_html(payload: dict, user_name: str = "Trader") -> str:
    """Render a Resend-friendly inline-styled HTML body for the digest."""
    stats = payload.get("stats") or {}
    best = payload.get("best_trade") or {}
    worst = payload.get("worst_trade") or {}
    reflection = payload.get("ai_reflection") or payload.get("suggested_action") or ""
    holds = payload.get("hold_reasons") or []
    heals = payload.get("auto_heal_breakdown") or []
    win_color = "#00FF41" if (stats.get("pnl_total") or 0) >= 0 else "#FF3030"
    days = payload.get("window_days") or 7

    rows_html = "".join(
        f'<tr><td style="padding:6px 10px;color:#A1A1AA;font-family:monospace;font-size:11px;'
        f'border-bottom:1px solid #1F1F1F;">{h["label"]}</td>'
        f'<td style="padding:6px 10px;color:#FAFAFA;font-family:monospace;font-size:11px;'
        f'border-bottom:1px solid #1F1F1F;text-align:right;">{h["count"]}×</td></tr>'
        for h in holds[:5]
    ) or '<tr><td colspan="2" style="padding:8px;color:#52525B;font-family:monospace;font-size:11px;text-align:center;">No HOLDs in window</td></tr>'

    heal_html = ", ".join(f"{h['kind']} ({h['count']}×)" for h in heals[:5]) or "None"

    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#0A0A0A;font-family:-apple-system,sans-serif;">
<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#0A0A0A;padding:24px;">
<tr><td align="center">
  <table cellpadding="0" cellspacing="0" border="0" width="560" style="background:#050505;border:1px solid #1F1F1F;">
    <tr><td style="padding:24px 28px;border-bottom:1px solid #1F1F1F;">
      <div style="font-family:monospace;font-size:10px;color:#52525B;letter-spacing:3px;">STOIC · WEEKLY DIGEST</div>
      <div style="font-family:monospace;font-size:22px;color:#FAFAFA;letter-spacing:-1px;margin-top:6px;">
        Hello {user_name}, here is your last {days} days
      </div>
    </td></tr>
    <tr><td style="padding:24px 28px;">
      <table cellpadding="0" cellspacing="0" border="0" width="100%">
        <tr>
          <td style="padding:6px 0;font-family:monospace;font-size:10px;color:#52525B;letter-spacing:2px;">NET P&amp;L</td>
          <td style="padding:6px 0;font-family:monospace;font-size:18px;color:{win_color};text-align:right;">${stats.get('pnl_total', 0):.2f}</td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-family:monospace;font-size:10px;color:#52525B;letter-spacing:2px;">TRADES · WIN RATE</td>
          <td style="padding:6px 0;font-family:monospace;font-size:14px;color:#FAFAFA;text-align:right;">{stats.get('trades', 0)} · {stats.get('win_rate', 0)}%</td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-family:monospace;font-size:10px;color:#52525B;letter-spacing:2px;">BEST</td>
          <td style="padding:6px 0;font-family:monospace;font-size:11px;color:#00FF41;text-align:right;">{best.get('symbol') or '—'} {('+$%.2f' % best.get('pnl', 0)) if best else ''}</td>
        </tr>
        <tr>
          <td style="padding:6px 0;font-family:monospace;font-size:10px;color:#52525B;letter-spacing:2px;">WORST</td>
          <td style="padding:6px 0;font-family:monospace;font-size:11px;color:#FF3030;text-align:right;">{worst.get('symbol') or '—'} {('-$%.2f' % abs(worst.get('pnl', 0))) if worst else ''}</td>
        </tr>
      </table>
    </td></tr>
    <tr><td style="padding:20px 28px;border-top:1px solid #1F1F1F;">
      <div style="font-family:monospace;font-size:10px;color:#52525B;letter-spacing:3px;margin-bottom:12px;">WHY THE BOT HELD OFF</div>
      <table cellpadding="0" cellspacing="0" border="0" width="100%">{rows_html}</table>
    </td></tr>
    <tr><td style="padding:20px 28px;border-top:1px solid #1F1F1F;">
      <div style="font-family:monospace;font-size:10px;color:#52525B;letter-spacing:3px;margin-bottom:8px;">AUTO-HEAL</div>
      <div style="font-family:monospace;font-size:11px;color:#A1A1AA;">{heal_html}</div>
    </td></tr>
    <tr><td style="padding:24px 28px;border-top:1px solid #1F1F1F;background:#0A0A0A;">
      <div style="font-family:monospace;font-size:10px;color:#52525B;letter-spacing:3px;margin-bottom:10px;">REFLECTION</div>
      <div style="font-family:Georgia,serif;font-size:14px;color:#FAFAFA;line-height:1.6;white-space:pre-wrap;">{reflection}</div>
    </td></tr>
    <tr><td style="padding:14px 28px;border-top:1px solid #1F1F1F;text-align:center;">
      <div style="font-family:monospace;font-size:9px;color:#52525B;letter-spacing:2px;">STOIC · {payload.get('generated_at', '')[:10]}</div>
    </td></tr>
  </table>
</td></tr></table>
</body></html>"""


@router.post("/weekly-digest/email")
async def email_weekly_digest(days: int = 7, user=Depends(get_current_user)):
    """Render the weekly digest with AI reflection and email it to the user.

    Returns: {ok: bool, email_id?: str, error?: str, configured: bool}
    """
    if not email_is_configured():
        return {"ok": False, "configured": False,
                "error": "RESEND_API_KEY not configured on the backend"}

    recipient = user.get("email")
    if not recipient:
        raise HTTPException(status_code=400, detail="User has no email on file")

    # Build the same payload as the GET, with AI reflection enabled.
    payload = await weekly_digest(days=days, include_ai=True, user=user)
    html = _render_digest_html(payload, user_name=user.get("name") or "Trader")
    plain_fallback = (payload.get("ai_reflection")
                      or payload.get("suggested_action") or "")
    subject = f"STOIC · Weekly Digest · {payload.get('window_days', 7)}-day recap"

    result = await send_email(
        recipient=recipient, subject=subject,
        html=html, text=plain_fallback,
    )
    return {**result, "configured": True, "recipient": recipient}
