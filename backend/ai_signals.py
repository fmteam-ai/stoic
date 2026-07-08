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
from risk import get_profile, derive_sl_tp, compute_kelly_position_size
from news import score_sentiment
from microstructure import current_session, session_bias_for, classify_regime, is_market_closed
from economic_calendar import macro_freeze_check, upcoming_for
from entropy_filter import classify_noise
from regime_adapter import adapt_profile_for_regime
from meta_labeler import predict_true_signal_probability
from feature_compressor import compress_history
from mtf_check import multi_timeframe_gate
from mtf_tiers import compute_mtf_tiers
from breakout_scalper import compute_breakout_scalper
from vwap_pullback import compute_vwap_pullback
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
- Multi-Timeframe context (`mtf_tiers`):
    • SHORT tier  ≈ intraday / H4 proxy  (5/10 SMA, RSI-7, ~last week)
    • MEDIUM tier ≈ D1 structure        (20/50 SMA, RSI-14, ~last month)
    • LONG tier   ≈ W1 / structural     (50/200 SMA, RSI-21, ~last quarter+)
    Use these together: a high-conviction BUY needs MEDIUM and LONG agreeing
    UP, ideally with SHORT also turning UP for entry timing. If LONG=DOWN
    while SHORT=UP you are fighting the structural trend — prefer HOLD.
    `alignment.dominant` summarises the vote; `all_aligned_up/down` flags
    the strongest setups.
- Breakout Scalper (`breakout_scalper`):
    Donchian-20 break + ≥0.25 ATR confirmation. If `signal=BUY/SELL` and
    aligned with MTF trend, treat as a continuation entry — boost confidence
    by ~5-10 points. If `signal=NONE` ignore. Counter-trend breakouts
    (e.g. signal=BUY but LONG tier DOWN) are usually fake-outs — do NOT
    boost on those.
- VWAP Pullback (`vwap_pullback`):
    Rolling 20-bar VWAP proxy. `regime=near` + trend-aligned `pullback_signal`
    = high-quality re-entry. `above_extended` / `below_extended` regimes
    (>1.5% from VWAP) are mean-reversion-risk — prefer HOLD or fade.
- Intraday M15 (`intraday_m15`, null when EA stream is stale):
    LIVE intraday structure from the broker's M15 feed — this is TODAY's
    price action which the daily tiers cannot see. Fields: trend (EMA20/50
    stack), momentum_3h_pct, donchian20 breakout, session_vwap distance,
    swing_structure (HH_HL bullish / LH_LL bearish), atr15, day_range_pct.
    When day_range_pct > 1.5% AND trend/donchian/structure agree on a
    direction, this is a TRADEABLE INTRADAY TREND DAY: you may issue
    BUY/SELL at 60-75 confidence in that direction even when daily MEDIUM/
    LONG tiers conflict — the system will tag it as an intraday scalp with
    tight M15-ATR stops. Never fight strong intraday momentum with a
    counter-trend entry.
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
                         min_conf_override: int = 0,
                         aggressive_mode: bool = False,
                         range_scalp_mode: bool = False) -> dict:
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
    from intraday_features import fetch_intraday_pack, intraday_alignment
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
    # ------------------------------------------------------------------------
    # MARKET-HOURS HARD VETO (iter-63) — applied BEFORE aggressive_mode bypass.
    # When the symbol's market is closed, broker will reject any order with
    # MT5 error 10018 MARKET_CLOSED. There is no LLM gymnastics that fixes
    # a closed exchange. So we short-circuit HOLD unconditionally.
    # XAUUSD/forex: Fri 21:00 UTC → Sun 22:00 UTC.  Crypto: never closed.
    # ------------------------------------------------------------------------
    market_closure = is_market_closed(symbol)

    # CHEAP HOLD pre-filter (iter-39) — skip the LLM call entirely when the
    # deterministic gates already guarantee HOLD. Saves ~$0.01-0.03 per call
    # plus 2-5s latency. The Claude pass would have HOLD'd anyway via the
    # 10-layer veto cascade below; this just short-circuits earlier.
    # ------------------------------------------------------------------------
    cheap_hold_reason = None
    # Market closure ALWAYS wins — even aggressive_mode cannot trade a
    # closed exchange. Other soft vetoes remain gated by aggressive_mode.
    if market_closure:
        cheap_hold_reason = (
            f"{market_closure['reason']} "
            f"(reopens in {market_closure['reopens_in_hours']}h)"
        )
    elif not aggressive_mode:
        if macro.get("frozen"):
            cheap_hold_reason = f"Macro freeze in effect: {macro.get('reason') or 'high-impact event window'}"
        elif regime.get("regime") == "CHOP":
            cheap_hold_reason = "Regime CHOP — high volatility without direction"
        elif not entropy.get("tradeable", True):
            cheap_hold_reason = f"Noise filter: {entropy.get('label', 'NOISY')} (entropy={entropy.get('entropy')})"

    if cheap_hold_reason:
        # Build the same response shape the LLM path would, with HOLD baked in.
        # Skip every veto / sizing path because final_action is locked to HOLD.
        return {
            "symbol": symbol,
            "action": "HOLD",
            "chart_action": "HOLD",
            "aggressive_applied": False,
            "confidence": 0,
            "entry_price": None,
            "stop_loss": None,
            "take_profit": None,
            "tp1": None, "tp2": None, "tp3": None,
            "sl_pips": 0, "tp_pips": [0, 0, 0],
            "lot_size": 0, "kelly_f": 0, "effective_risk_pct": 0, "risk_amount": 0,
            "risk_level": risk_level,
            "reasoning": (
                f"CHEAP HOLD pre-filter: {cheap_hold_reason}. "
                f"Skipping LLM call to save tokens — the veto cascade would have "
                f"reached the same conclusion."
            ),
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
            "key_factors": [cheap_hold_reason],
            "min_confidence_required": adapted_profile["min_confidence"],
            "veto_applied": True,
            "cheap_hold": True,  # tag for cost analytics / UI badge
            "market_closure": market_closure,  # populated iff this HOLD was triggered by closed market
            "tradeable": False,
            "created_at": datetime.now(timezone.utc),
        }

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
    # iter-123/124b · Range Scalp gets FIRST claim: in a confirmed M15 RANGE
    # a deterministic edge-fade beats an indicator-bias trend override (which
    # would fire into the range and rightly die at the consensus gate).
    trade_scope = "swing"
    range_scalp_applied = ""
    if range_scalp_mode and action == "HOLD" and not macro.get("frozen"):
        from intraday_features import range_scalp_signal
        rs_action, rs_note = range_scalp_signal(intraday_pack)
        if rs_action:
            action = rs_action
            trade_scope = "range_scalp"
            confidence = max(confidence, adapted_profile["min_confidence"] + 3)
            range_scalp_applied = rs_note
    if aggressive_mode and action == "HOLD" and confidence >= 15 and not macro.get("frozen"):
        bias_votes = 0
        kv = indicators.get("kalman_velocity")
        if isinstance(kv, (int, float)) and kv != 0:
            bias_votes += 1 if kv > 0 else -1
        cp = indicators.get("current_price")
        ma200 = indicators.get("ma_200") or indicators.get("ma200") or indicators.get("sma_200")
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

    # iter-123 · Range Scalp scope (moved above the aggressive override —
    # runs first so a confirmed range fade outranks the trend override).
    # Trend vetoes (MTF/CHOP/entropy/short-tier/A+) don't apply to that scope;
    # every capital protection (risk caps, cooldowns, news freeze) still does.

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
    if regime_label == "CHOP" and action != "HOLD" and trade_scope != "range_scalp":
        regime_veto = "Regime CHOP detected — high vol without direction. Trade vetoed."
        final_action = "HOLD"

    # 2b. SELF-CONTRADICTION veto — when the LLM's own reasoning explicitly
    # says the trade is blocked but `action` came back BUY/SELL anyway, trust
    # the prose, not the tag. This caught a P0 footgun in iter-48 where the
    # bot fired 5 losing SELLs in 22 min while its reasoning literally read
    # "CAUTIOUS_WAIT mode blocks new positions". See PRD.md iter-48.
    #
    # IMPORTANT: this veto deliberately IGNORES `aggressive_mode`. The flag
    # exists to relax *probabilistic* filters (entropy, learned classifier).
    # A model contradicting itself is a sanity bug, not a probability — no
    # amount of "aggressive" setting should let the bot trade on it.
    self_contra_veto = ""
    if action != "HOLD" and trade_scope != "range_scalp":
        rtxt = (parsed.get("reasoning") or "").lower()
        block_phrases = [
            "cautious_wait", "cautious wait",
            "blocks new positions", "blocks trading", "blocks trade execution",
            "non-tradeable environment", "do not enter", "do not trade",
            "no entry", "mandates no entry", "mandates patience",
            "prohibit entry", "prohibits entry",
            "red-light noise filter blocks",
        ]
        hits = [p for p in block_phrases if p in rtxt]
        if hits:
            self_contra_veto = (
                f"Self-contradiction veto: model emitted {action} but its own "
                f"reasoning contains blocking language: {hits[:2]}. Honouring prose."
            )
            final_action = "HOLD"

    # 3. Macro-event freeze veto
    macro_veto = ""
    if macro.get("frozen") and action != "HOLD":
        macro_veto = macro["reason"]
        final_action = "HOLD"

    # 4. Shannon entropy noise veto — block trades in chaotic/random markets
    entropy_veto = ""
    if (not entropy.get("tradeable", True) and action != "HOLD"
            and not aggressive_mode and trade_scope != "range_scalp"):
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
    mtf = multi_timeframe_gate(action, history, indicators, mtf_tiers=mtf_tiers)
    mtf_veto = ""
    if (action != "HOLD" and not mtf["aligned"] and not aggressive_mode
            and trade_scope != "range_scalp"):
        # iter-120 · Intraday scalp override — daily tiers conflict, but if
        # the live M15 structure STRONGLY supports the action this becomes a
        # tagged intraday scalp (tight M15-ATR geometry) instead of a veto.
        ia_score, ia_note = intraday_alignment(action, intraday_pack)
        if ia_score >= 60:
            trade_scope = "intraday_scalp"
            mtf = {**mtf, "scalp_override": (
                f"MTF unaligned but M15 strongly supports {action} "
                f"(score {ia_score}/100: {ia_note}) — intraday scalp scope.")}
        else:
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
    if (final_action != "HOLD" and not confluence["passed"] and not aggressive_mode
            and trade_scope != "range_scalp"):
        aplus_veto = confluence["reason"]
        final_action = "HOLD"

    # ---- SL/TP — adaptive ATR-based, with hardcoded pip-system fallback ----
    sl, tp1, tp2, tp3 = current_price, current_price, current_price, current_price
    atr = indicators.get("atr_14") or 0.0
    # iter-120 · Intraday scalps size stops off M15 ATR (much tighter than
    # daily ATR) — same 1.5/5.0 multipliers preserve the weighted R:R ≈ 2.08.
    atr15 = float((intraday_pack or {}).get("atr15") or 0)
    if trade_scope in ("intraday_scalp", "range_scalp") and atr15 > 0 and final_action in ("BUY", "SELL"):
        if trade_scope == "range_scalp":
            # Fade toward VWAP: tight stop beyond the extreme, target clamped
            # so the weighted R:R stays ≥ ~1.25 (high-win-rate style).
            sl_dist_price = 1.2 * atr15
            vwap = (intraday_pack or {}).get("session_vwap")
            vwap_dist = abs(current_price - float(vwap)) if vwap else 0.0
            tp_dist_price = max(2.4 * atr15, min(4.8 * atr15, vwap_dist or 3.0 * atr15))
        else:
            sl_dist_price = ATR_SL_MULTIPLIER * atr15
            tp_dist_price = ATR_TP_MULTIPLIER * atr15
        sl_min_price = pips_to_price(symbol, 30)   # scalp floor: 30 pips
        sl_max_price = pips_to_price(symbol, SL_MAX_PIPS)
        sl_dist_price = max(sl_min_price, min(sl_max_price, sl_dist_price))
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
    elif ADAPTIVE_SL_TP_ENABLED and atr > 0 and final_action in ("BUY", "SELL"):
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
        # Aggressive Mode: floor 1.1; range scalps: 1.05 (high-win-rate style).
        if trade_scope == "range_scalp":
            rr_floor = 1.05
        else:
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

    # 11. Intraday counter-momentum gate (iter-57) — the MTF tiers are built
    # from DAILY bars and can't see today's session. Blocks trades fighting a
    # strong intraday move vs yesterday's close (2026-07-06: 147/147 SELLs
    # while gold rallied +0.8% intraday).
    from payoff_guard import intraday_counter_momentum, short_tier_momentum_veto
    intraday_veto, intraday_change_pct = intraday_counter_momentum(
        final_action, symbol, current_price, history)
    if intraday_veto and trade_scope == "range_scalp":
        # Fading a range extreme always opposes the day's tape — that's the
        # whole setup. Breakout protection lives in range_scalp_signal itself
        # (requires FLAT trend + Donchian INSIDE).
        intraday_veto = ""
    if intraday_veto:
        final_action = "HOLD"

    # 12. SHORT-tier momentum veto (iter-59) — V-recovery failure mode: the
    # MEDIUM/LONG tiers stay bearish for weeks after a sharp reversal, so the
    # 2-of-3 vote keeps approving fades of a fresh strong rally. When the
    # last-week trend is strongly directional (|slope| ≥ 1%), fading it is
    # forbidden regardless of the slower tiers.
    short_tier_veto = (short_tier_momentum_veto(final_action, mtf_tiers)
                       if trade_scope != "range_scalp" else None)
    short_tier_defer = ""
    if short_tier_veto:
        # iter-120b · The SHORT tier is a WEEKLY daily-bar slope — when live
        # M15 structure strongly confirms the trade (score ≥60), the "fresh
        # rally" it protects against is already over on the intraday chart.
        ia_score, ia_note = intraday_alignment(final_action, intraday_pack)
        if ia_score >= 60:
            short_tier_defer = (
                f"Short-tier veto deferred: weekly slope opposes {final_action}, "
                f"but live M15 strongly confirms it ({ia_score}/100: {ia_note}).")
            short_tier_veto = ""
        else:
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
    if intraday_veto:
        reasoning = f"{reasoning}\n\nVETO (intraday-momentum): {intraday_veto}"
    if short_tier_veto:
        reasoning = f"{reasoning}\n\nVETO (short-tier-momentum): {short_tier_veto}"
    if short_tier_defer:
        reasoning = f"{reasoning}\n\nNOTE: {short_tier_defer}"
    if range_scalp_applied:
        reasoning = f"{reasoning}\n\nRANGE SCALP: {range_scalp_applied}"
    if self_contra_veto:
        reasoning = f"{reasoning}\n\nVETO (self-contradiction): {self_contra_veto}"

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
        "scope": trade_scope,
        "range_scalp_applied": range_scalp_applied or None,
        "intraday_m15": intraday_pack,
        "mtf_gate": mtf,
        "mtf_tiers": mtf_tiers,
        "breakout_scalper": breakout,
        "vwap_pullback": vwap,
        "learned_meta": learned_meta,
        "aplus_confluence": confluence,
        "rr_ratio": rr_ratio,
        "kalman_filter": kalman_feat,
        "cot_positioning": cot_feat,
        "real_yield_10y": tips_feat,
        "dxy": dxy_feat,
        "dxy_gate": dxy_gate,
        "intraday_momentum": {"change_pct": intraday_change_pct,
                              "veto": bool(intraday_veto)},
        "short_tier_momentum_veto": bool(short_tier_veto),
        "liquidity_window": liquidity_window,
        "key_factors": parsed.get("key_factors", []),
        "min_confidence_required": adapted_profile["min_confidence"],
        "veto_applied": bool(veto_reason) or bool(regime_veto) or bool(self_contra_veto) or bool(macro_veto) or bool(entropy_veto) or bool(meta_veto) or bool(mtf_veto) or bool(learned_veto) or bool(aplus_veto) or bool(rr_veto) or bool(dxy_veto) or bool(intraday_veto) or bool(short_tier_veto),
        "tradeable": final_action != "HOLD" and confidence >= adapted_profile["min_confidence"],
        "created_at": datetime.now(timezone.utc),
    }
