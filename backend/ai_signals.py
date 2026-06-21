"""AI-driven signal generation using Claude Sonnet 4.5 via emergentintegrations."""
import os
import json
import uuid
import re
from datetime import datetime, timezone
from emergentintegrations.llm.chat import LlmChat, UserMessage

from market import get_quote, get_history, compute_indicators, asset_type_of
from risk import get_profile, derive_sl_tp, compute_position_size

SYSTEM_PROMPT = """You are an institutional-grade quantitative trading analyst.
You analyse market data (6-month history + live quote + technical indicators) and emit one trading signal per symbol.

Rules:
- Output STRICT JSON only — no prose, no markdown, no code fences.
- Schema: {"action":"BUY"|"SELL"|"HOLD","confidence":0-100,"reasoning":"...","key_factors":["...","..."]}
- Reasoning: 2-3 concise sentences citing the indicators that drove the decision.
- key_factors: 2-4 short bullets (max 8 words each).
- Confidence reflects your conviction; HOLD typically <50.
- Consider trend (SMA20/50/200), momentum (RSI), volatility, and distance from 6-month high/low.
- Be conservative: prefer HOLD when signals are mixed.
"""


def _parse_ai_json(text: str) -> dict:
    """Robustly extract JSON from a model response."""
    text = (text or "").strip()
    # Strip ```json fences if any
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    # Find first { ... } block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    try:
        return json.loads(text)
    except Exception:
        return {
            "action": "HOLD",
            "confidence": 0,
            "reasoning": "Failed to parse model response.",
            "key_factors": [],
        }


async def analyze_symbol(symbol: str, risk_level: str) -> dict:
    """Generate an AI trading signal for one symbol.

    Returns full signal payload ready for storage (no DB write here).
    """
    profile = get_profile(risk_level)
    quote = await get_quote(symbol)
    history = await get_history(symbol)
    indicators = compute_indicators(history) or {}
    current_price = quote.get("price") or indicators.get("current_price") or 0.0

    # Build a compact, factual prompt
    user_text = json.dumps({
        "symbol": symbol,
        "asset_type": asset_type_of(symbol),
        "live_quote": {
            "price": current_price,
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "change_pct": quote.get("change_pct"),
        },
        "indicators_6mo": indicators,
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

    # Derive SL / TP using ATR proxy (volatility * price)
    vol_pct = indicators.get("volatility_30d_pct") or 1.0
    atr_proxy = (vol_pct / 100.0) * current_price
    sl, tp = derive_sl_tp(action, current_price, atr_proxy, profile)

    # Lot sizing — assumes microcent equity of 1000 (refined by EA on execute)
    sl_distance = abs(current_price - sl) or 0.0001
    lot_size = compute_position_size(equity=1000.0,
                                     risk_pct=profile["risk_pct"],
                                     sl_pips=sl_distance,
                                     pip_value=1.0)

    return {
        "symbol": symbol,
        "action": action,
        "confidence": round(confidence, 1),
        "entry_price": round(current_price, 5),
        "stop_loss": sl,
        "take_profit": tp,
        "lot_size": lot_size,
        "risk_level": risk_level,
        "reasoning": parsed.get("reasoning", ""),
        "indicators": indicators,
        "key_factors": parsed.get("key_factors", []),
        "min_confidence_required": profile["min_confidence"],
        "tradeable": action != "HOLD" and confidence >= profile["min_confidence"],
        "created_at": datetime.now(timezone.utc),
    }
