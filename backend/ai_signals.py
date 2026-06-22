"""AI-driven signal generation: Multi-Engine Consensus + Meta-Labeler.

Three-tier verification (per 2026 best practice):
  Engine 1 (Quant)      : indicators + regime + entropy
  Engine 2 (Semantic)   : Claude Sonnet 4.5 + news sentiment
  Engine 3 (Meta-Label) : binary classifier P(true_signal | engines)

Plus dynamic Regime Swapping — SL/TP/Kelly mutate based on live regime.
"""
import os
import json
import uuid
import re
from datetime import datetime, timezone
from emergentintegrations.llm.chat import LlmChat, UserMessage

from market import get_quote, get_history, compute_indicators, asset_type_of
from risk import get_profile, derive_sl_tp, compute_kelly_position_size
from news import score_sentiment
from microstructure import current_session, session_bias_for, classify_regime
from economic_calendar import macro_freeze_check, upcoming_for
from entropy_filter import classify_noise
from regime_adapter import adapt_profile_for_regime
from meta_labeler import predict_true_signal_probability
from feature_compressor import compress_history
from mtf_check import multi_timeframe_gate

SYSTEM_PROMPT = """You are an institutional-grade quantitative trading analyst.
Inputs: live quote, 12-month indicator snapshot, current news sentiment score,
trading session context, regime classification, compressed long-history features
(spectral/autocorrelation/skew), and current regime execution mode.

Rules:
- Output STRICT JSON only — no prose, no markdown, no code fences.
- Schema: {"action":"BUY"|"SELL"|"HOLD","confidence":0-100,"reasoning":"...","key_factors":["...","..."]}
- Reasoning: 2-3 concise sentences citing indicators, sentiment, session AND regime.
- key_factors: 2-4 short bullets (max 8 words each).
- Confidence reflects your conviction; HOLD typically <50.
- Regime guide:
    HIGH_VOL_TREND  -> momentum entries favoured (system runs DYNAMIC_MOMENTUM mode)
    LOW_VOL_TREND   -> safe trend entries (system runs DEFENSIVE_SCALP mode)
    RANGE           -> mean-reversion at extremes (DEFENSIVE_SCALP mode)
    CHOP            -> always HOLD (system will block anyway)
    TRANSITIONAL    -> wait for confirmation
- Session guide: respect the symbol-session bias when sizing conviction.
- Use compressed_history_features (acf, spectral bands, kurtosis) for context.
- Note: a Meta-Labeler will re-verify your output; conservative is safer.
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
    session = current_session()
    session_bias = session_bias_for(symbol, session)
    regime = classify_regime(indicators)
    macro = await macro_freeze_check(symbol)
    upcoming_macro = await upcoming_for(symbol, hours=24)
    # Shannon entropy noise filter — closes used for distribution analysis
    entropy = classify_noise([c["close"] for c in history]) if history else {
        "entropy": 0, "label": "ORGANIZED", "traffic_light": "green", "tradeable": True, "threshold": 0.9
    }
    # O(N) feature compressor — Mamba/SSM substitute for long-sequence stats
    compressed_features = compress_history(history)

    # --- Regime-Adaptive Risk Modifier — swap execution mode by live regime ---
    adapted_profile, regime_meta = adapt_profile_for_regime(profile, regime)

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
        "session": {**session, **session_bias},
        "regime": regime,
        "upcoming_macro_events_24h": [
            {"title": e["title"], "country": e["country"], "impact": e["impact"], "when": e["when"]}
            for e in upcoming_macro[:5]
        ],
        "noise_filter": entropy,
        "compressed_history_features": compressed_features,
        "regime_execution_mode": regime_meta,
        "risk_profile": {
            "level": risk_level,
            "min_confidence_to_trade": adapted_profile["min_confidence"],
            "max_risk_pct_per_trade": adapted_profile["risk_pct"],
            "kelly_cap": adapted_profile["kelly_cap"],
            "regime_adapted": True,
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

    # Veto cascade — each veto checks its own condition independently,
    # so reasoning carries all reasons we held off. Final action is HOLD if any fires.
    final_action = action

    # 1. Dual-AI sentiment veto (chart says trade, news strongly disagrees)
    sentiment_action, veto_reason = _apply_dual_veto(action, confidence, sentiment)
    if veto_reason:
        final_action = "HOLD"

    # 2. Regime CHOP safety veto
    regime_label = regime.get("regime")
    regime_veto = ""
    if regime_label == "CHOP" and action != "HOLD":
        regime_veto = "Regime CHOP detected — high vol without direction. Trade vetoed."
        final_action = "HOLD"

    # 3. Macro-event freeze veto
    macro_veto = ""
    if macro.get("frozen") and action != "HOLD":
        macro_veto = macro["reason"]
        final_action = "HOLD"

    # 4. Shannon entropy noise veto — block trades in chaotic/random markets
    entropy_veto = ""
    if not entropy.get("tradeable", True) and action != "HOLD":
        entropy_veto = (
            f"Noise filter: market entropy={entropy.get('entropy')} "
            f"({entropy.get('label')}). Random walk regime — trade vetoed."
        )
        final_action = "HOLD"

    # 5. Meta-Labeler veto — independent fake-out classifier on engines 1+2
    meta_label = predict_true_signal_probability(
        action=action,
        confidence=confidence,
        sentiment=sentiment,
        regime=regime,
        entropy=entropy,
        session={**session, **session_bias},
        indicators=indicators,
        upcoming_macro=upcoming_macro,
    )
    meta_veto = ""
    if action != "HOLD" and meta_label["verdict"] == "FAKE_OUT":
        meta_veto = (
            f"Meta-Labeler classified this as FAKE_OUT "
            f"(p_true={meta_label['p_true']:.2f} < {meta_label['threshold']}). "
            f"Engine consensus too weak — trade vetoed."
        )
        final_action = "HOLD"

    # 6. Multi-Timeframe trend confluence gate
    mtf = multi_timeframe_gate(action, history, indicators)
    mtf_veto = ""
    if action != "HOLD" and not mtf["aligned"]:
        mtf_veto = mtf["reason"]
        final_action = "HOLD"

    # SL/TP — pip-based fixed targets (150 SL, 100/200/300 TP tiers)
    from pip_utils import pips_to_price
    sl_pips_target = 150
    tp1_pips_target = 100
    tp2_pips_target = 200
    tp3_pips_target = 300
    sl_distance_price = pips_to_price(symbol, sl_pips_target)
    if final_action == "BUY":
        sl = round(current_price - sl_distance_price, 5)
        tp1 = round(current_price + pips_to_price(symbol, tp1_pips_target), 5)
        tp2 = round(current_price + pips_to_price(symbol, tp2_pips_target), 5)
        tp3 = round(current_price + pips_to_price(symbol, tp3_pips_target), 5)
    elif final_action == "SELL":
        sl = round(current_price + sl_distance_price, 5)
        tp1 = round(current_price - pips_to_price(symbol, tp1_pips_target), 5)
        tp2 = round(current_price - pips_to_price(symbol, tp2_pips_target), 5)
        tp3 = round(current_price - pips_to_price(symbol, tp3_pips_target), 5)
    else:
        # HOLD — no actionable targets; derive defaults from current price
        sl, tp1, tp2, tp3 = current_price, current_price, current_price, current_price
    tp = tp3  # legacy `take_profit` field points to the furthest target

    # Kelly-modified position sizing — use regime-adapted profile
    sl_distance = abs(current_price - sl) or 0.0001
    sizing = compute_kelly_position_size(
        equity=1000.0,
        confidence_pct=confidence,
        sl_pips=sl_distance,
        profile=adapted_profile,
        pip_value=1.0,
    )

    reasoning = parsed.get("reasoning", "")
    if veto_reason:
        reasoning = f"{reasoning}\n\nVETO (news): {veto_reason}"
    if regime_veto:
        reasoning = f"{reasoning}\n\nVETO (regime): {regime_veto}"
    if macro_veto:
        reasoning = f"{reasoning}\n\nVETO (macro): {macro_veto}"
    if entropy_veto:
        reasoning = f"{reasoning}\n\nVETO (entropy): {entropy_veto}"
    if meta_veto:
        reasoning = f"{reasoning}\n\nVETO (meta-labeler): {meta_veto}"
    if mtf_veto:
        reasoning = f"{reasoning}\n\nVETO (multi-timeframe): {mtf_veto}"

    return {
        "symbol": symbol,
        "action": final_action,
        "chart_action": action,
        "confidence": round(confidence, 1),
        "entry_price": round(current_price, 5),
        "stop_loss": sl,
        "take_profit": tp,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "sl_pips": sl_pips_target,
        "tp_pips": [tp1_pips_target, tp2_pips_target, tp3_pips_target],
        "lot_size": sizing["lot_size"],
        "kelly_f": sizing["kelly_f"],
        "effective_risk_pct": sizing["effective_risk_pct"],
        "risk_amount": sizing["risk_amount"],
        "risk_level": risk_level,
        "reasoning": reasoning,
        "indicators": indicators,
        "sentiment": sentiment,
        "session": session,
        "session_bias": session_bias,
        "regime": regime,
        "macro": macro,
        "upcoming_macro": upcoming_macro[:5],
        "noise_filter": entropy,
        "compressed_features": compressed_features,
        "regime_execution_mode": regime_meta,
        "meta_label": meta_label,
        "mtf_gate": mtf,
        "key_factors": parsed.get("key_factors", []),
        "min_confidence_required": adapted_profile["min_confidence"],
        "veto_applied": bool(veto_reason) or bool(regime_veto) or bool(macro_veto) or bool(entropy_veto) or bool(meta_veto) or bool(mtf_veto),
        "tradeable": final_action != "HOLD" and confidence >= adapted_profile["min_confidence"],
        "created_at": datetime.now(timezone.utc),
    }
