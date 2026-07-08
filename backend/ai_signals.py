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
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("ai_signals")
from emergentintegrations.llm.chat import LlmChat, UserMessage

from market import get_quote, get_history, compute_indicators, asset_type_of
from risk import get_profile, compute_kelly_position_size
from news import score_sentiment
from microstructure import current_session, session_bias_for, classify_regime, is_market_closed
from economic_calendar import macro_freeze_check, upcoming_for
from entropy_filter import classify_noise
from regime_adapter import adapt_profile_for_regime
from feature_compressor import compress_history
from mtf_tiers import compute_mtf_tiers
from breakout_scalper import compute_breakout_scalper
from vwap_pullback import compute_vwap_pullback
from kalman import kalman_features
from macro.cot import get_gold_positioning
from macro.tips import get_real_yield
from macro.dxy import get_dxy_snapshot
from mtf_intraday import fetch_mtf_confluence
from pip_utils import pips_to_price, price_to_pips

# --- Execution geometry tunables ---
ATR_SL_MULTIPLIER = float(os.environ.get("ATR_SL_MULTIPLIER", "1.5"))
ATR_TP_MULTIPLIER = float(os.environ.get("ATR_TP_MULTIPLIER", "5.0"))   # gives weighted R:R ≈ 2.08x
SL_MIN_PIPS = float(os.environ.get("SL_MIN_PIPS", "80"))
SL_MAX_PIPS = float(os.environ.get("SL_MAX_PIPS", "250"))
MTF_RR_FLOOR = float(os.environ.get("MTF_RR_FLOOR", "1.1"))

NARRATOR_PROMPT = """You are the explanation layer of STOIC, an institutional-grade
multi-timeframe trading system. The system has ALREADY confirmed a trade using its
strict top-down cascade (4H trend → 1H structure → M15 pullback → live-price
breakout). Your job is to explain the confirmed setup to the trader — you do NOT
decide whether to trade.

Rules:
- Output STRICT JSON only — no prose, no markdown, no code fences.
- Schema: {"reasoning":"...","key_factors":["...","..."]}
- reasoning: 2-4 concise sentences explaining WHY this cascade setup is valid,
  citing the 4H/1H trend, the M15 pullback geometry, the breakout level, and any
  supporting context (session, news sentiment, indicators).
- If the broader context (news, macro, indicators) CONFLICTS with the setup,
  say so honestly — separate risk guards handle vetoes.
- key_factors: 2-4 short bullets (max 8 words each).
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


async def _intraday_entropy_override(symbol: str, daily: dict) -> dict:
    """When daily entropy says NOISY, check the EA's fresh M15 stream.
    An organized intraday market (clean trend) outranks stale daily noise."""
    try:
        from database import get_db
        from pip_utils import base_symbol
        doc = await get_db().intraday_candles.find_one(
            {"symbol": base_symbol(symbol)},
            {"bars": {"$slice": -80}, "updated_at": 1},
            sort=[("updated_at", -1)],
        )
        if not doc:
            return daily
        upd = doc.get("updated_at")
        if upd:
            u = datetime.fromisoformat(str(upd).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - u > timedelta(minutes=30):
                return daily  # stale stream — keep the daily verdict
        closes = [float(b.get("c") or 0) for b in (doc.get("bars") or []) if b.get("c")]
        if len(closes) < 31:
            return daily
        intraday = classify_noise(closes)
        if intraday.get("tradeable"):
            return {
                **intraday,
                "source": "intraday_m15_override",
                "daily_entropy": daily.get("entropy"),
                "note": (f"Daily entropy {daily.get('entropy')} NOISY, but live M15 "
                         f"structure is {intraday.get('label')} "
                         f"(entropy {intraday.get('entropy')}) — intraday verdict wins."),
            }
        return {**daily, "intraday_entropy": intraday.get("entropy")}
    except Exception:
        return daily


async def analyze_symbol(symbol: str, risk_level: str,
                         min_conf_override: int = 0) -> dict:
    profile = get_profile(risk_level)
    quote = await get_quote(symbol)
    history = await get_history(symbol)
    # iter-117 · Live-price patch — refresh the in-flight daily bar with the
    # live quote so intraday indicators track the real market instead of the
    # history cache (bot missed a 60-pt gold drop analyzing a stale close).
    live_px = float(quote.get("price") or 0)
    if history and live_px > 0:
        lb = dict(history[-1])
        lb["close"] = live_px
        lb["high"] = max(float(lb.get("high") or live_px), live_px)
        lb["low"] = min(float(lb.get("low") or live_px), live_px)
        history = history[:-1] + [lb]
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
    # iter-117 · Intraday entropy override — daily-return entropy cannot see
    # a fresh intraday trend forming inside a single day. When the EA's live
    # M15 stream shows an ORGANIZED market, it outranks the daily verdict.
    if not entropy.get("tradeable", True):
        entropy = await _intraday_entropy_override(symbol, entropy)
    # iter-120 · Intraday M15 feature pack — live EA-stream vision so the
    # strategy engine can trade clean intraday days the daily tiers can't see.
    from intraday_features import fetch_intraday_pack
    intraday_pack = await fetch_intraday_pack(symbol)
    # O(N) feature compressor — Mamba/SSM substitute for long-sequence stats
    compressed_features = compress_history(history)

    # Multi-timeframe tier pack — structured short/medium/long indicator
    # snapshots from the same daily series, consumed by both the LLM prompt
    # and the MTF gate below (iter-67).
    mtf_tiers = compute_mtf_tiers(history)

    # iter-69 · Breakout Scalper feature (Donchian-20 + ATR confirmation)
    breakout = compute_breakout_scalper(history)
    # iter-69 · VWAP-proxy pullback feature, contextualised by HTF trend
    vwap = compute_vwap_pullback(
        history, htf_trend=(mtf_tiers.get("alignment") or {}).get("dominant", "FLAT")
    )

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
        "intraday_m15": intraday_pack,
        "compressed_history_features": compressed_features,
        "mtf_tiers": mtf_tiers,
        "breakout_scalper": breakout,
        "vwap_pullback": vwap,
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

    # ------------------------------------------------------------------------
    # EXECUTION ENGINE — single strategy (iter-126 simplification).
    # The strict Multi-Timeframe cascade (4H trend → 1H structure → M15
    # pullback → live-price breakout) is the ONLY signal generator. Claude is
    # the explanation layer for confirmed setups — it never picks direction.
    # Hard capital protections kept here: market closed, macro freeze,
    # dual-AI news veto, minimum R:R. Account-level guards (drawdown,
    # cooldowns, anti-tilt, spread, payoff guard) run in the bot runner.
    # ------------------------------------------------------------------------
    market_closure = is_market_closed(symbol)
    mtf_conf = None if market_closure else await fetch_mtf_confluence(symbol, current_price)

    def _hold(reason: str, closure: dict | None = None) -> dict:
        return {
            "symbol": symbol,
            "action": "HOLD",
            "chart_action": "HOLD",
            "confidence": 0,
            "entry_price": None, "stop_loss": None, "take_profit": None,
            "tp1": None, "tp2": None, "tp3": None,
            "sl_pips": 0, "tp_pips": [0, 0, 0],
            "lot_size": 0, "kelly_f": 0, "effective_risk_pct": 0, "risk_amount": 0,
            "risk_level": risk_level,
            "reasoning": reason,
            "scope": "mtf_confluence",
            "mtf_confluence": mtf_conf,
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
            "meta_label": None,
            "mtf_gate": None,
            "mtf_tiers": mtf_tiers,
            "intraday_m15": intraday_pack,
            "breakout_scalper": breakout,
            "vwap_pullback": vwap,
            "learned_meta": None,
            "aplus_confluence": None,
            "rr_ratio": None,
            "kalman_filter": kalman_feat,
            "cot_positioning": cot_feat,
            "real_yield_10y": tips_feat,
            "dxy": dxy_feat,
            "dxy_gate": None,
            "liquidity_window": liquidity_window,
            "key_factors": [reason[:120]],
            "min_confidence_required": adapted_profile["min_confidence"],
            "veto_applied": True,
            "cheap_hold": True,
            "market_closure": closure,
            "tradeable": False,
            "created_at": datetime.now(timezone.utc),
        }

    if market_closure:
        return _hold(
            f"{market_closure['reason']} (reopens in {market_closure['reopens_in_hours']}h)",
            closure=market_closure,
        )
    if macro.get("frozen"):
        return _hold(f"Macro freeze in effect: {macro.get('reason') or 'high-impact event window'}")
    if not mtf_conf:
        return _hold(
            "MTF cascade: no fresh M15 stream from the EA (bridge must be online "
            "and streaming candles) — standing by."
        )
    if not mtf_conf.get("aligned"):
        return _hold(f"MTF cascade: {mtf_conf.get('note') or 'timeframes not aligned'} — standing by.")

    # ---- CONFLUENCE CONFIRMED — build the trade ----------------------------
    action = mtf_conf["direction"]
    trade_scope = "mtf_confluence"
    confidence = float(adapted_profile["min_confidence"] + 5)

    reasoning = f"MTF CONFLUENCE: {mtf_conf['note']}"
    key_factors = [
        f"4H trend {mtf_conf.get('h4_trend')}",
        f"1H structure {mtf_conf.get('h1_structure')}",
        f"Live break of {mtf_conf.get('swing_level')}",
    ]
    try:
        chat = LlmChat(
            api_key=os.environ["EMERGENT_LLM_KEY"],
            session_id=f"signal-{symbol}-{uuid.uuid4().hex[:8]}",
            system_message=NARRATOR_PROMPT,
        ).with_model("anthropic", "claude-sonnet-4-5-20250929")
        narration = await chat.send_message(UserMessage(text=json.dumps({
            "confirmed_setup": {"direction": action, "cascade": mtf_conf},
            "market_context": json.loads(user_text),
        }, separators=(",", ":"))))
        parsed = _parse_ai_json(str(narration))
        if parsed.get("reasoning"):
            reasoning = f"{reasoning}\n\n{parsed['reasoning']}"
        if parsed.get("key_factors"):
            key_factors = parsed["key_factors"]
    except Exception as e:  # noqa: BLE001
        logger.warning("Narration pass failed for %s: %s", symbol, e)

    final_action = action

    # Guard A — dual-AI news veto: strongly opposed news blocks the entry.
    _, veto_reason = _apply_dual_veto(action, confidence, sentiment)
    if veto_reason:
        final_action = "HOLD"

    # ---- SL/TP geometry — M15 ATR preferred, daily ATR fallback ------------
    atr15 = float((intraday_pack or {}).get("atr15") or 0)
    atr_daily = float(indicators.get("atr_14") or 0)
    if atr15 > 0:
        sl_dist_price = ATR_SL_MULTIPLIER * atr15
        tp_dist_price = ATR_TP_MULTIPLIER * atr15
        sl_min_price = pips_to_price(symbol, 30)   # intraday floor: 30 pips
    else:
        sl_dist_price = ATR_SL_MULTIPLIER * atr_daily
        tp_dist_price = ATR_TP_MULTIPLIER * atr_daily
        sl_min_price = pips_to_price(symbol, SL_MIN_PIPS)
    sl_max_price = pips_to_price(symbol, SL_MAX_PIPS)
    sl_dist_price = max(sl_min_price, min(sl_max_price, sl_dist_price))
    if tp_dist_price <= 0:
        tp_dist_price = sl_dist_price * 2.5
    tp1_dist = tp_dist_price * 0.4
    tp2_dist = tp_dist_price * 0.7
    tp3_dist = tp_dist_price * 1.0
    if action == "BUY":
        sl = round(current_price - sl_dist_price, 5)
        tp1 = round(current_price + tp1_dist, 5)
        tp2 = round(current_price + tp2_dist, 5)
        tp3 = round(current_price + tp3_dist, 5)
    else:
        sl = round(current_price + sl_dist_price, 5)
        tp1 = round(current_price - tp1_dist, 5)
        tp2 = round(current_price - tp2_dist, 5)
        tp3 = round(current_price - tp3_dist, 5)
    tp = tp3
    sl_pips_target = round(price_to_pips(symbol, sl_dist_price), 1)
    tp1_pips_target = round(price_to_pips(symbol, tp1_dist), 1)
    tp2_pips_target = round(price_to_pips(symbol, tp2_dist), 1)
    tp3_pips_target = round(price_to_pips(symbol, tp3_dist), 1)

    # Guard B — minimum weighted R:R (50%@TP1 + 25%@TP2 + 25%@TP3 vs SL).
    sl_dist = abs(current_price - sl) or 1e-9
    weighted_tp_dist = (
        0.5 * abs(tp1 - current_price)
        + 0.25 * abs(tp2 - current_price)
        + 0.25 * abs(tp3 - current_price)
    )
    rr_ratio = round(weighted_tp_dist / sl_dist, 2)
    rr_veto = ""
    if rr_ratio < MTF_RR_FLOOR:
        rr_veto = (f"Weighted R:R {rr_ratio} < min {MTF_RR_FLOOR}. "
                   f"Expected value too low — trade vetoed.")
        final_action = "HOLD"

    # Kelly-modified position sizing — regime-adapted profile
    sizing = compute_kelly_position_size(
        equity=1000.0,
        confidence_pct=confidence,
        sl_pips=sl_dist,
        profile=adapted_profile,
        pip_value=1.0,
    )

    if veto_reason:
        reasoning = f"{reasoning}\n\nVETO (news): {veto_reason}"
    if rr_veto:
        reasoning = f"{reasoning}\n\nVETO (R:R): {rr_veto}"

    is_hold = final_action == "HOLD"
    return {
        "symbol": symbol,
        "action": final_action,
        "chart_action": action,
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
        "scope": trade_scope,
        "mtf_confluence": mtf_conf,
        "mtf_confluence_applied": mtf_conf.get("note"),
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
        "meta_label": None,
        "mtf_gate": None,
        "mtf_tiers": mtf_tiers,
        "intraday_m15": intraday_pack,
        "breakout_scalper": breakout,
        "vwap_pullback": vwap,
        "learned_meta": None,
        "aplus_confluence": None,
        "rr_ratio": rr_ratio,
        "kalman_filter": kalman_feat,
        "cot_positioning": cot_feat,
        "real_yield_10y": tips_feat,
        "dxy": dxy_feat,
        "dxy_gate": None,
        "liquidity_window": liquidity_window,
        "key_factors": key_factors,
        "min_confidence_required": adapted_profile["min_confidence"],
        "veto_applied": bool(veto_reason) or bool(rr_veto),
        "tradeable": final_action != "HOLD" and confidence >= adapted_profile["min_confidence"],
        "created_at": datetime.now(timezone.utc),
    }
