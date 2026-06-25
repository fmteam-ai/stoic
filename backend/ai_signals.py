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
import logging
from datetime import datetime, timezone

logger = logging.getLogger("ai_signals")
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
from kalman import kalman_features
from macro.cot import get_gold_positioning
from macro.tips import get_real_yield
from macro.dxy import get_dxy_snapshot, dxy_gate_check
from learned_meta import predict_p_win as learned_predict_p_win
from confluence import confluence_check
from pip_utils import pips_to_price, price_to_pips

# --- A+ Selectivity tunables (the WR-vs-frequency knobs) ---
APLUS_MIN_CONFLUENCES = int(os.environ.get("APLUS_MIN_CONFLUENCES", "4"))   # 0-6
MIN_RR_RATIO = float(os.environ.get("MIN_RR_RATIO", "2.0"))                 # TP / SL distance
ADAPTIVE_SL_TP_ENABLED = os.environ.get("ADAPTIVE_SL_TP_ENABLED", "true").lower() == "true"
ATR_SL_MULTIPLIER = float(os.environ.get("ATR_SL_MULTIPLIER", "1.5"))
ATR_TP_MULTIPLIER = float(os.environ.get("ATR_TP_MULTIPLIER", "5.0"))   # gives weighted R:R ≈ 2.08x
SL_MIN_PIPS = float(os.environ.get("SL_MIN_PIPS", "80"))
SL_MAX_PIPS = float(os.environ.get("SL_MAX_PIPS", "250"))

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
- Gold-specific (XAUUSD only): when `kalman_filter`, `cot_positioning`, and
  `real_yield_10y` are present in the payload, use them as macro context:
    • kalman_filter.k_velocity > 0  → smoothed price is rising; aligns with BUY.
    • cot_positioning.overcrowded_long == true → speculative positioning is
      saturated long; raise the bar on new BUYs (consider HOLD or fade).
    • cot_positioning.overcrowded_short == true → contrarian BUY setup if
      other factors align.
    • real_yield_10y.regime == "bullish_gold" → real yields falling → gold
      tailwind; bias slightly toward BUY confidence. "bearish_gold" → bias
      toward SELL / HOLD.
    • dxy_dollar_index.regime == "bullish_usd" → DXY above 20-EMA and rising;
      strong inverse-correlation headwind for XAU longs. Prefer SELL / HOLD.
    • dxy_dollar_index.regime == "bearish_usd" → DXY below 20-EMA and falling;
      tailwind for XAU longs. Bias toward BUY.
    • A downstream DXY gate will hard-veto any XAU trade that fights the
      dollar's prevailing direction — make sure your action is consistent.
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


async def analyze_symbol(symbol: str, risk_level: str,
                         min_conf_override: int = 0,
                         aggressive_mode: bool = False) -> dict:
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

    # Gold-specific institutional features (no-op on non-gold or on failure)
    kalman_feat = kalman_features([c["close"] for c in history]) if history else {}
    cot_feat = None
    tips_feat = None
    dxy_feat = None
    if symbol.upper() == "XAUUSD":
        try:
            cot_feat = await get_gold_positioning()
        except Exception:
            cot_feat = None  # never block signal generation on auxiliary feed
        try:
            tips_feat = await get_real_yield()
        except Exception:
            tips_feat = None
        try:
            dxy_feat = await get_dxy_snapshot()
        except Exception:
            dxy_feat = None

    # --- Regime-Adaptive Risk Modifier — swap execution mode by live regime ---
    adapted_profile, regime_meta = adapt_profile_for_regime(profile, regime)

    # --- Liquidity-window booster (XAUUSD only) ---
    # Gold trends cleanest during London/NY overlap (13:00-16:00 UTC) — tighten
    # the confidence floor a touch in that window to capture the best setups;
    # raise it in off-hours where slippage and chop tax everything.
    # No-op for non-XAU symbols.
    liquidity_window = {
        "primary": session["primary"],
        "high_volume_overlap": bool(session.get("is_high_volume_window")),
        "confidence_adjustment": 0,
    }
    if symbol.upper() == "XAUUSD":
        base_min = adapted_profile["min_confidence"]
        if session.get("is_high_volume_window"):
            # Lower floor by 3 pts (clamped at 70) — best liquidity, tightest spreads.
            new_min = max(70, base_min - 3)
            liquidity_window["confidence_adjustment"] = new_min - base_min
            adapted_profile = {**adapted_profile, "min_confidence": new_min}
        elif session["primary"] in ("off-hours",) and not session.get("is_weekend"):
            # Raise floor by +4 (clamped at 92) — off-hours = wider spreads.
            new_min = min(92, base_min + 4)
            liquidity_window["confidence_adjustment"] = new_min - base_min
            adapted_profile = {**adapted_profile, "min_confidence": new_min}

    # User-level threshold override (lowest priority — applied after all profile/session adjustments)
    if 0 < min_conf_override < 100:
        adapted_profile = {**adapted_profile, "min_confidence": int(min_conf_override)}

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
        # Gold-specific institutional intel (None on non-gold)
        "kalman_filter": kalman_feat,
        "cot_positioning": ({
            "managed_money_net": cot_feat.get("managed_money_net"),
            "net_pct": cot_feat.get("managed_money_net_pct"),
            "percentile_52w": cot_feat.get("net_pct_percentile_52w"),
            "overcrowded_long": cot_feat.get("overcrowded_long"),
            "overcrowded_short": cot_feat.get("overcrowded_short"),
        } if cot_feat else None),
        "real_yield_10y": ({
            "value": tips_feat.get("real_yield_10y"),
            "delta_5d": tips_feat.get("delta_5d"),
            "regime": tips_feat.get("regime"),
        } if tips_feat else None),
        "dxy_dollar_index": ({
            "current": dxy_feat.get("current_price"),
            "ema_20": dxy_feat.get("ema_20"),
            "slope_5d_pct": dxy_feat.get("slope_5d_pct"),
            "regime": dxy_feat.get("regime"),
        } if dxy_feat else None),
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

    # ------------------------------------------------------------------------
    # AGGRESSIVE MODE — opt-in override:
    # When Claude returns HOLD with any non-zero confidence and macro isn't
    # frozen, infer a directional bias from underlying indicators (Kalman
    # velocity sign + price-vs-MA200 + macro DXY bias) and convert HOLD to
    # BUY/SELL. Increases trade frequency at the cost of per-trade edge.
    # The 10-layer veto cascade still runs after this — so macro/regime/spread
    # vetoes can still block the trade.
    # ------------------------------------------------------------------------
    aggressive_applied = None
    if aggressive_mode and action == "HOLD" and confidence >= 15 and not macro.get("frozen"):
        bias_votes = 0
        kv = indicators.get("kalman_velocity")
        if isinstance(kv, (int, float)) and kv != 0:
            bias_votes += 1 if kv > 0 else -1
        cp = indicators.get("current_price")
        ma200 = indicators.get("ma_200") or indicators.get("ma200")
        if cp and ma200:
            bias_votes += 1 if cp > ma200 else -1
        dxy_dir = (dxy_feat or {}).get("regime") if dxy_feat else None
        if dxy_dir == "bullish_usd":
            bias_votes -= 1  # bullish USD → bearish gold
        elif dxy_dir == "bearish_usd":
            bias_votes += 1
        logger.info("Aggressive Mode eval sym=%s claude=%s conf=%s votes=%d kv=%s cp_vs_ma200=%s dxy=%s",
                    symbol, action, confidence, bias_votes, kv,
                    "above" if (cp and ma200 and cp > ma200) else "below" if (cp and ma200) else "?",
                    dxy_dir)
        if bias_votes >= 1:
            action = "BUY"
            confidence = max(confidence, adapted_profile["min_confidence"] + 3)
            aggressive_applied = "BUY (aggressive override · indicator bias bullish)"
        elif bias_votes <= -1:
            action = "SELL"
            confidence = max(confidence, adapted_profile["min_confidence"] + 3)
            aggressive_applied = "SELL (aggressive override · indicator bias bearish)"
    elif aggressive_mode:
        logger.info("Aggressive Mode skipped sym=%s action=%s conf=%s frozen=%s",
                    symbol, action, confidence, macro.get("frozen"))

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
    if not entropy.get("tradeable", True) and action != "HOLD" and not aggressive_mode:
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
    if action != "HOLD" and meta_label["verdict"] == "FAKE_OUT" and not aggressive_mode:
        meta_veto = (
            f"Meta-Labeler classified this as FAKE_OUT "
            f"(p_true={meta_label['p_true']:.2f} < {meta_label['threshold']}). "
            f"Engine consensus too weak — trade vetoed."
        )
        final_action = "HOLD"

    # 6. Multi-Timeframe trend confluence gate
    mtf = multi_timeframe_gate(action, history, indicators)
    mtf_veto = ""
    if action != "HOLD" and not mtf["aligned"] and not aggressive_mode:
        mtf_veto = mtf["reason"]
        final_action = "HOLD"

    # 7. Learned Meta-Classifier — local logistic regression P(win | features).
    #    Only fires once we have a trained artifact (≥30 closed trades).
    learned_meta = None
    learned_veto = ""
    try:
        # Build a snapshot view the classifier can read
        learned_input = {
            "action": action, "confidence": confidence,
            "entry_price": current_price,
            "kalman_filter": kalman_feat,
            "cot_positioning": cot_feat,
            "real_yield_10y": tips_feat,
            "mtf_gate": mtf,
            "upcoming_macro": upcoming_macro,
        }
        learned_meta = await learned_predict_p_win(learned_input)
    except Exception:
        learned_meta = None
    if learned_meta and action != "HOLD" and learned_meta["verdict"] == "REJECT" and not aggressive_mode:
        learned_veto = (
            f"Learned classifier: p_win={learned_meta['p_win']:.2f} "
            f"< {learned_meta['threshold']:.2f} "
            f"(trained on {learned_meta['n_samples']} trades, "
            f"AUC={learned_meta['train_auc']:.2f}). Trade vetoed."
        )
        final_action = "HOLD"

    # 8. A+ Confluence filter — only let through high-conviction setups
    confluence = confluence_check(
        action=final_action,
        symbol=symbol,
        mtf=mtf,
        learned_meta=learned_meta,
        cot=cot_feat,
        tips=tips_feat,
        session={**session, **session_bias},
        history=history,
        indicators=indicators,
        min_confluences=APLUS_MIN_CONFLUENCES,
    )
    aplus_veto = ""
    if final_action != "HOLD" and not confluence["passed"] and not aggressive_mode:
        aplus_veto = confluence["reason"]
        final_action = "HOLD"

    # ---- SL/TP — adaptive ATR-based, with hardcoded pip-system fallback ----
    sl, tp1, tp2, tp3 = current_price, current_price, current_price, current_price
    atr = indicators.get("atr_14") or 0.0
    if ADAPTIVE_SL_TP_ENABLED and atr > 0 and final_action in ("BUY", "SELL"):
        # SL = clamp(ATR_SL_MULTIPLIER × ATR, SL_MIN_PIPS, SL_MAX_PIPS)
        sl_dist_price = ATR_SL_MULTIPLIER * atr
        # Convert clamp bounds from pips → price
        sl_min_price = pips_to_price(symbol, SL_MIN_PIPS)
        sl_max_price = pips_to_price(symbol, SL_MAX_PIPS)
        sl_dist_price = max(sl_min_price, min(sl_max_price, sl_dist_price))
        tp_dist_price = ATR_TP_MULTIPLIER * atr
        # Scaling tiers across three TP levels (preserves the partial-close system)
        tp1_dist = tp_dist_price * 0.4
        tp2_dist = tp_dist_price * 0.7
        tp3_dist = tp_dist_price * 1.0
        if final_action == "BUY":
            sl = round(current_price - sl_dist_price, 5)
            tp1 = round(current_price + tp1_dist, 5)
            tp2 = round(current_price + tp2_dist, 5)
            tp3 = round(current_price + tp3_dist, 5)
        else:
            sl = round(current_price + sl_dist_price, 5)
            tp1 = round(current_price - tp1_dist, 5)
            tp2 = round(current_price - tp2_dist, 5)
            tp3 = round(current_price - tp3_dist, 5)
        sl_pips_target = round(price_to_pips(symbol, sl_dist_price), 1)
        tp1_pips_target = round(price_to_pips(symbol, tp1_dist), 1)
        tp2_pips_target = round(price_to_pips(symbol, tp2_dist), 1)
        tp3_pips_target = round(price_to_pips(symbol, tp3_dist), 1)
    elif final_action in ("BUY", "SELL"):
        # Hardcoded legacy pip-system fallback (150 SL, 100/200/300 TP)
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
        else:
            sl = round(current_price + sl_distance_price, 5)
            tp1 = round(current_price - pips_to_price(symbol, tp1_pips_target), 5)
            tp2 = round(current_price - pips_to_price(symbol, tp2_pips_target), 5)
            tp3 = round(current_price - pips_to_price(symbol, tp3_pips_target), 5)
    else:
        sl_pips_target = tp1_pips_target = tp2_pips_target = tp3_pips_target = 0
    tp = tp3

    # 9. Minimum Reward-to-Risk gate — kill setups with poor expectancy.
    # Use weighted-average TP distance (50%@TP1 + 25%@TP2 + 25%@TP3) vs SL.
    rr_veto = ""
    rr_ratio = None
    if final_action in ("BUY", "SELL"):
        sl_dist = abs(current_price - sl) or 1e-9
        weighted_tp_dist = (
            0.5 * abs(tp1 - current_price)
            + 0.25 * abs(tp2 - current_price)
            + 0.25 * abs(tp3 - current_price)
        )
        rr_ratio = round(weighted_tp_dist / sl_dist, 2)
        # In Aggressive Mode, drop the R:R floor to 1.1 (still positive expectancy).
        rr_floor = 1.1 if aggressive_mode else MIN_RR_RATIO
        if rr_ratio < rr_floor:
            rr_veto = (
                f"Weighted R:R {rr_ratio} < min {rr_floor}. "
                f"Expected value too low — trade vetoed."
            )
            final_action = "HOLD"

    # 10. DXY inverse-correlation gate (XAUUSD only) — block trades fighting the dollar.
    # Aggressive Mode already used DXY direction to set the action, so skip this veto.
    dxy_gate = dxy_gate_check(final_action, symbol, dxy_feat)
    dxy_veto = ""
    if final_action in ("BUY", "SELL") and not dxy_gate["passed"] and not aggressive_mode:
        dxy_veto = dxy_gate["reason"]
        final_action = "HOLD"

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
    if learned_veto:
        reasoning = f"{reasoning}\n\nVETO (learned-meta): {learned_veto}"
    if aplus_veto:
        reasoning = f"{reasoning}\n\nVETO (A+ confluence): {aplus_veto}"
    if rr_veto:
        reasoning = f"{reasoning}\n\nVETO (R:R): {rr_veto}"
    if dxy_veto:
        reasoning = f"{reasoning}\n\nVETO (DXY gate): {dxy_veto}"

    # On HOLD, clear price levels so consumers (UI, execution) don't see
    # a misleading entry=SL=TP that would be a zero-risk trade if forced
    # through. tradeable=False already prevents execution, but data should
    # match semantics.
    is_hold = final_action == "HOLD"
    return {
        "symbol": symbol,
        "action": final_action,
        "chart_action": action,
        "aggressive_applied": aggressive_applied,
        "confidence": round(confidence, 1),
        "entry_price": None if is_hold else round(current_price, 5),
        "stop_loss": None if is_hold else sl,
        "take_profit": None if is_hold else tp,
        "tp1": None if is_hold else tp1,
        "tp2": None if is_hold else tp2,
        "tp3": None if is_hold else tp3,
        "sl_pips": 0 if is_hold else sl_pips_target,
        "tp_pips": [0, 0, 0] if is_hold else [tp1_pips_target, tp2_pips_target, tp3_pips_target],
        "lot_size": 0 if is_hold else sizing["lot_size"],
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
        "learned_meta": learned_meta,
        "aplus_confluence": confluence,
        "rr_ratio": rr_ratio,
        "kalman_filter": kalman_feat,
        "cot_positioning": cot_feat,
        "real_yield_10y": tips_feat,
        "dxy": dxy_feat,
        "dxy_gate": dxy_gate,
        "liquidity_window": liquidity_window,
        "key_factors": parsed.get("key_factors", []),
        "min_confidence_required": adapted_profile["min_confidence"],
        "veto_applied": bool(veto_reason) or bool(regime_veto) or bool(macro_veto) or bool(entropy_veto) or bool(meta_veto) or bool(mtf_veto) or bool(learned_veto) or bool(aplus_veto) or bool(rr_veto) or bool(dxy_veto),
        "tradeable": final_action != "HOLD" and confidence >= adapted_profile["min_confidence"],
        "created_at": datetime.now(timezone.utc),
    }
