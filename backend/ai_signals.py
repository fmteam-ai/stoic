"""AI-driven signal generation: dual-AI (technical + news sentiment) with veto."""
import os
import json
import uuid
import re
from datetime import datetime, timezone
from emergentintegrations.llm.chat import LlmChat, UserMessage

from market import get_quote, get_history, compute_indicators, asset_type_of
from risk import get_profile, derive_sl_tp, compute_position_size
from news import score_sentiment

SYSTEM_PROMPT = """You are an institutional-grade quantitative trading analyst.
Inputs you receive: live quote, 12 months of daily history-derived indicators, and current news sentiment score.

Rules:
- Output STRICT JSON only — no prose, no markdown, no code fences.
- Schema: {"action":"BUY"|"SELL"|"HOLD","confidence":0-100,"reasoning":"...","key_factors":["...","..."]}
- Reasoning: 2-3 concise sentences citing both indicators AND sentiment that drove the decision.
- key_factors: 2-4 short bullets (max 8 words each).
- Confidence reflects your conviction; HOLD typically <50.
- Consider trend (SMA20/50/200), momentum (RSI), volatility, distance from 12-month high/low, AND news sentiment.
- Be conservative: prefer HOLD when signals are mixed.
"""


def _parse_ai_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except Exception:
        return {"action": "HOLD", "confidence": 0,
                "reasoning": "Failed to parse model response.", "key_factors": []}


def _apply_dual_veto(action: str, confidence: float, sentiment: dict) -> tuple:
    """Hard veto rule: if Chart AI and News AI strongly disagree, force HOLD.

    'Strongly' means sentiment score has magnitude >= 0.5 AND opposes the action.
    Returns (final_action, veto_reason or '').
    """
    score = float(sentiment.get("score") or 0)
    if abs(score) < 0.5:
        return action, ""
    if action == "BUY" and score <= -0.5:
        return "HOLD", f"News sentiment is strongly bearish ({score}); chart BUY vetoed."
    if action == "SELL" and score >= 0.5:
        return "HOLD", f"News sentiment is strongly bullish ({score}); chart SELL vetoed."
    return action, ""


async def analyze_symbol(symbol: str, risk_level: str) -> dict:
    profile = get_profile(risk_level)
    quote = await get_quote(symbol)
    history = await get_history(symbol)
    indicators = compute_indicators(history) or {}
    sentiment = await score_sentiment(symbol)
    current_price = quote.get("price") or indicators.get("current_price") or 0.0

    user_text = json.dumps({
        "symbol": symbol,
        "asset_type": asset_type_of(symbol),
        "live_quote": {
            "price": current_price,
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "change_pct": quote.get("change_pct"),
        },
        "indicators_12mo": indicators,
        "news_sentiment": {
            "score": sentiment.get("score"),
            "label": sentiment.get("label"),
            "summary": sentiment.get("summary"),
            "article_count": sentiment.get("article_count"),
        },
        "risk_profile": {
            "level": risk_level,
            "min_confidence_to_trade": profile["min_confidence"],
            "risk_pct_per_trade": profile["risk_pct"],
        },
    }, separators=(",", ":"))

    chat = LlmChat(
        api_key=os.environ["EMERGENT_LLM_KEY"],
        session_id=f"signal-{symbol}-{uuid.uuid4().hex[:8]}",
        system_message=SYSTEM_PROMPT,
    ).with_model("anthropic", "claude-sonnet-4-5-20250929")

    response = await chat.send_message(UserMessage(text=user_text))
    parsed = _parse_ai_json(str(response))

    action = (parsed.get("action") or "HOLD").upper()
    if action not in ("BUY", "SELL", "HOLD"):
        action = "HOLD"
    confidence = float(parsed.get("confidence") or 0)

    # Dual-AI veto: chart x sentiment alignment check
    final_action, veto_reason = _apply_dual_veto(action, confidence, sentiment)

    # Derive SL / TP using ATR-like proxy
    vol_pct = indicators.get("volatility_30d_pct") or 1.0
    atr_proxy = (vol_pct / 100.0) * current_price
    sl, tp = derive_sl_tp(final_action, current_price, atr_proxy, profile)

    sl_distance = abs(current_price - sl) or 0.0001
    lot_size = compute_position_size(equity=1000.0,
                                     risk_pct=profile["risk_pct"],
                                     sl_pips=sl_distance,
                                     pip_value=1.0)

    reasoning = parsed.get("reasoning", "")
    if veto_reason:
        reasoning = f"{reasoning}\n\nVETO: {veto_reason}"

    return {
        "symbol": symbol,
        "action": final_action,
        "chart_action": action,
        "confidence": round(confidence, 1),
        "entry_price": round(current_price, 5),
        "stop_loss": sl,
        "take_profit": tp,
        "lot_size": lot_size,
        "risk_level": risk_level,
        "reasoning": reasoning,
        "indicators": indicators,
        "sentiment": sentiment,
        "key_factors": parsed.get("key_factors", []),
        "min_confidence_required": profile["min_confidence"],
        "veto_applied": bool(veto_reason),
        "tradeable": final_action != "HOLD" and confidence >= profile["min_confidence"],
        "created_at": datetime.now(timezone.utc),
    }
