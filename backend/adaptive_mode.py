"""
Adaptive Trading Mode — STOIC's "max win-rate + auto-adapt" subsystem.

This module bundles three orthogonal adaptiveness layers that overlay on
top of any existing bot_config. None of them is enabled by default — each
is opt-in via a single boolean field on the bot_config so users can A/B
them independently.

  Phase 1 · Profit-Taking Mode + Max-TP Cap
    profit_taking_mode ∈ {"expected_value", "win_rate", "trend_follow"}
      - expected_value (default)  → keep existing ATR-based TPs and
        partial-close/trailing R-multiples. No mutation.
      - win_rate                  → shrink TPs to ≤ max_tp_pips (or 100 by
        default) AND tighten partial-close to 0.5R/70% AND tight trail.
        Optimises for # winning trades, not avg win size.
      - trend_follow              → widen partial-close (1.5R/30%) and
        trailing (2R / 1R) so runners stretch.
    max_tp_pips_per_symbol: per-symbol hard ceiling (in pips). 0/missing =
    uncapped. Cap is enforced AFTER profit_taking_mode shaping so users
    can dial it independently.

  Phase 2 · Rolling Adaptive Risk
    adaptive_risk_enabled (bool). When true the bot scales the user's
    risk_pct by a multiplier derived from the last-N closed trades' win
    rate. Multiplier table:
        win_rate < 40%   → 0.5×   (capital-protect mode)
        40-50%           → 0.7×
        50-60%           → 1.0×   (neutral)
        60-70%           → 1.15×
        ≥ 70%            → 1.3×   (press the edge)
    Requires ≥ 5 closed trades in window; otherwise neutral.

  Phase 3 · Regime-Aware Auto-Preset
    auto_preset_enabled (bool). When true, each tick the bot inspects the
    signal's regime_execution_mode and overlays the matching preset onto
    the cfg in-memory (DB unchanged). User's explicit preset selection is
    ignored only while auto is enabled. Mapping:
        AGGRESSIVE / TRENDING       → trend_rider
        DEFENSIVE_SCALP             → fast_scalp
        CAUTIOUS_WAIT / TRANSITIONAL → scalper
        RANGING                     → mean_reversion
        else                        → balanced

All functions are pure — DB I/O is isolated to compute_risk_multiplier()
which awaits a closed-trade lookup.
"""
from __future__ import annotations

import logging
from typing import Optional

from database import get_db
from pip_utils import pips_to_price, price_to_pips
from strategy_presets import PRESETS

logger = logging.getLogger(__name__)


# ───────────────────────── Phase 1 ─────────────────────────

# Profit-taking shaping knobs (apply to cfg overlay, NOT persisted to DB).
PROFIT_TAKING_OVERLAYS: dict[str, dict] = {
    "win_rate": {
        "partial_close_enabled": True,
        "partial_close_trigger_r": 0.5,
        "partial_close_fraction": 0.7,
        "trailing_enabled": True,
        "trailing_start_r": 0.5,
        "trailing_distance_r": 0.25,
        "breakeven_enabled": True,
        "breakeven_trigger_r": 0.4,
    },
    "trend_follow": {
        "partial_close_enabled": True,
        "partial_close_trigger_r": 1.5,
        "partial_close_fraction": 0.3,
        "trailing_enabled": True,
        "trailing_start_r": 2.0,
        "trailing_distance_r": 1.0,
        "breakeven_enabled": True,
        "breakeven_trigger_r": 1.0,
    },
    # "expected_value" → no overlay (use cfg as-is)
}

# Default TP cap when profit_taking_mode == "win_rate" but the user
# hasn't set an explicit per-symbol cap. Keeps the mode useful out of
# the box without forcing UI configuration.
DEFAULT_WIN_RATE_TP_CAP_PIPS = 100.0

# iter-77 · "Smart Cap" — the 100-pip TP cap fires ONLY in choppy regimes
# (CAUTIOUS_WAIT / DEFENSIVE_SCALP / TRANSITIONAL / RANGING). In TRENDING
# or AGGRESSIVE regimes the cap REMOVES itself so winners stretch like
# Plan A — this fixes the iter-74 problem where the cap was too tight in
# clean trend regimes (per-trade profit was ~10× smaller than Plan A's).
# The user can still enforce a hard cap via max_tp_pips_per_symbol — that
# per-symbol explicit cap is honored regardless of regime.
CHOPPY_REGIMES = {"DEFENSIVE_SCALP", "CAUTIOUS_WAIT", "TRANSITIONAL", "RANGING"}
TRENDING_REGIMES = {"TRENDING", "AGGRESSIVE"}
# Multiplier still applied INSIDE a choppy regime so the 100p default
# tightens to 60p when conditions are particularly noisy.
CHOPPY_REGIME_TP_MULTIPLIER = 0.6


def _clip_tp_to_pip_cap(signal: dict, cap_pips: float) -> dict:
    """Return a *new* signal dict with `take_profit`, `tp3`, `tp_pips[2]`
    clipped so |entry → tp| ≤ cap_pips. tp1 and tp2 are also clipped if
    they exceed the cap (rare). Leaves SL untouched."""
    entry = signal.get("entry_price")
    tp = signal.get("take_profit")
    action = signal.get("action")
    symbol = signal.get("symbol")
    if entry is None or tp is None or action not in ("BUY", "SELL") or not symbol:
        return signal
    cap_price = pips_to_price(symbol, cap_pips)
    if cap_price <= 0:
        return signal

    direction = 1 if action == "BUY" else -1
    max_tp = entry + direction * cap_price

    def _clip(level: Optional[float]) -> Optional[float]:
        if level is None:
            return None
        # For BUY, cap is upper bound; for SELL, lower bound.
        if direction == 1:
            return min(level, max_tp)
        return max(level, max_tp)

    new_sig = dict(signal)
    new_sig["take_profit"] = round(_clip(tp), 5)
    # Clip the per-tier TPs too (used by partial-close / trailing scaffolds).
    if signal.get("tp1") is not None:
        new_sig["tp1"] = round(_clip(signal["tp1"]), 5)
    if signal.get("tp2") is not None:
        new_sig["tp2"] = round(_clip(signal["tp2"]), 5)
    if signal.get("tp3") is not None:
        new_sig["tp3"] = round(_clip(signal["tp3"]), 5)
    # Refresh the pip distances for telemetry.
    tp_pips = list(signal.get("tp_pips") or [0, 0, 0])
    for i, key in enumerate(("tp1", "tp2", "tp3")):
        level = new_sig.get(key)
        if level is not None:
            tp_pips[i] = round(price_to_pips(symbol, abs(level - entry)), 1)
    new_sig["tp_pips"] = tp_pips
    return new_sig


def apply_profit_taking_mode(signal: dict, cfg: dict) -> tuple[dict, dict]:
    """Phase 1 entrypoint.
    Returns (effective_signal, effective_cfg) where:
      - effective_signal has TPs clipped per (profit_taking_mode,
        max_tp_pips_per_symbol, regime tightening)
      - effective_cfg is the cfg overlaid with the mode's
        partial-close / trailing / breakeven knobs.
    Both inputs are NEVER mutated."""
    mode = (cfg.get("profit_taking_mode") or "expected_value").lower()
    overlay = PROFIT_TAKING_OVERLAYS.get(mode, {})
    effective_cfg = {**cfg, **overlay} if overlay else dict(cfg)
    effective_cfg["_profit_taking_mode_applied"] = mode

    # Determine TP cap in pips
    sym = (signal.get("symbol") or "").upper()
    per_symbol_cap = (cfg.get("max_tp_pips_per_symbol") or {}).get(sym, 0)
    try:
        per_symbol_cap = float(per_symbol_cap) if per_symbol_cap else 0.0
    except (TypeError, ValueError):
        per_symbol_cap = 0.0

    regime_exec = (signal.get("regime_execution_mode") or {}).get("execution_mode")
    regime_str = str(regime_exec or "").upper()

    # iter-77 · Smart Cap precedence:
    #   1. Explicit per-symbol cap → ALWAYS honored (user knows what they want)
    #   2. win_rate mode default (100p) → applied ONLY in choppy regimes;
    #      removed in TRENDING/AGGRESSIVE so runners stretch.
    #   3. expected_value / trend_follow modes → no implicit cap.
    cap_pips = per_symbol_cap
    smart_cap_applied = False
    if cap_pips <= 0 and mode == "win_rate":
        if regime_str in CHOPPY_REGIMES:
            cap_pips = DEFAULT_WIN_RATE_TP_CAP_PIPS
            smart_cap_applied = True
        # else: TRENDING / AGGRESSIVE / unknown → no cap, winners run.

    # Regime-aware tightening (only applies when a cap is in force AND
    # regime is in the choppiest bucket — DEFENSIVE_SCALP / CAUTIOUS_WAIT)
    regime_tightened = False
    if cap_pips > 0 and regime_str in ("DEFENSIVE_SCALP", "CAUTIOUS_WAIT"):
        cap_pips = round(cap_pips * CHOPPY_REGIME_TP_MULTIPLIER, 1)
        regime_tightened = True

    effective_signal = signal
    if cap_pips > 0 and signal.get("action") in ("BUY", "SELL"):
        effective_signal = _clip_tp_to_pip_cap(signal, cap_pips)

    # Attach telemetry the dashboard / audit log can show
    effective_signal = dict(effective_signal)
    effective_signal["adaptive_profit_taking"] = {
        "mode": mode,
        "tp_cap_pips": cap_pips if cap_pips > 0 else None,
        "regime_tightened": regime_tightened,
        "smart_cap_applied": smart_cap_applied,
        "smart_cap_skipped_for_trend": (mode == "win_rate"
                                        and per_symbol_cap <= 0
                                        and regime_str in TRENDING_REGIMES),
        "regime_execution_mode": regime_exec,
    }
    return effective_signal, effective_cfg


# ───────────────────────── Phase 2 ─────────────────────────

# Inclusive lower bounds → multiplier.
RISK_MULTIPLIER_BUCKETS: list[tuple[float, float]] = [
    (70.0, 1.30),
    (60.0, 1.15),
    (50.0, 1.00),
    (40.0, 0.70),
    (0.0,  0.50),
]
MIN_SAMPLES_FOR_ADAPT = 5


def _multiplier_for(win_rate_pct: float) -> float:
    for lo, mult in RISK_MULTIPLIER_BUCKETS:
        if win_rate_pct >= lo:
            return mult
    return 0.5


async def compute_risk_multiplier(user_id: str, account_id: Optional[str] = None,
                                  window: int = 20) -> dict:
    """Return {'multiplier': float, 'win_rate_pct': float, 'samples': int,
    'reason': str}. Multiplier ∈ [0.5, 1.3]. Defaults to 1.0 when there
    aren't enough closed trades in the window to be statistically useful."""
    db = get_db()
    q = {"user_id": user_id, "status": "closed",
         "origin": {"$in": ["auto", "manual"]}}
    if account_id:
        q["account_id"] = account_id
    cursor = db.trades.find(q).sort("closed_at", -1).limit(int(window))
    rows = await cursor.to_list(length=int(window))
    samples = len(rows)
    if samples < MIN_SAMPLES_FOR_ADAPT:
        return {
            "multiplier": 1.0,
            "win_rate_pct": None,
            "samples": samples,
            "window": int(window),
            "reason": f"Insufficient data ({samples} closed < {MIN_SAMPLES_FOR_ADAPT}) — neutral risk.",
        }
    wins = sum(1 for r in rows if (r.get("pnl") or 0) > 0)
    losses = sum(1 for r in rows if (r.get("pnl") or 0) < 0)
    decided = wins + losses
    if decided == 0:
        return {"multiplier": 1.0, "win_rate_pct": None, "samples": samples,
                "window": int(window),
                "reason": "All recent trades closed flat — neutral risk."}
    win_rate = round(100.0 * wins / decided, 1)
    mult = _multiplier_for(win_rate)
    return {
        "multiplier": mult,
        "win_rate_pct": win_rate,
        "samples": samples,
        "window": int(window),
        "wins": wins,
        "losses": losses,
        "reason": (
            f"Last {decided} decided trades · win rate {win_rate}% "
            f"→ risk multiplier {mult}×"
        ),
    }


# ───────────────────────── Phase 3 ─────────────────────────

# Regime-execution-mode → preset key. Values that don't appear here fall
# through to "balanced" so the bot never ends up preset-less.
REGIME_TO_PRESET: dict[str, str] = {
    "AGGRESSIVE":      "trend_rider",
    "TRENDING":        "trend_rider",
    "DEFENSIVE_SCALP": "fast_scalp",
    "CAUTIOUS_WAIT":   "scalper",
    "TRANSITIONAL":    "scalper",
    "RANGING":         "mean_reversion",
    "MEAN_REVERSION":  "mean_reversion",
}
DEFAULT_AUTO_PRESET = "balanced"


def pick_preset_for_regime(execution_mode: Optional[str],
                           regime: Optional[str] = None) -> dict:
    """Pure helper. Returns {'preset_key': str, 'reason': str}.
    Inspects execution_mode first (more specific); falls back to regime."""
    key = None
    if execution_mode:
        key = REGIME_TO_PRESET.get(str(execution_mode).upper())
    if not key and regime:
        key = REGIME_TO_PRESET.get(str(regime).upper())
    if not key or key not in PRESETS:
        key = DEFAULT_AUTO_PRESET
    return {
        "preset_key": key,
        "reason": (
            f"execution_mode={execution_mode!r} regime={regime!r} → {key}"
        ),
    }


def apply_auto_preset(cfg: dict, signal: dict) -> tuple[dict, dict]:
    """Phase 3 entrypoint. If auto_preset_enabled is on, overlay the
    regime-matched preset's config onto cfg. Returns (effective_cfg,
    selection_info). Otherwise returns (cfg, {'enabled': False})."""
    if not cfg.get("auto_preset_enabled"):
        return cfg, {"enabled": False}
    regime_exec = (signal.get("regime_execution_mode") or {}).get("execution_mode")
    regime = (signal.get("regime") or {}).get("regime") if signal.get("regime") else None
    pick = pick_preset_for_regime(regime_exec, regime)
    preset = PRESETS.get(pick["preset_key"], {})
    overlay_cfg = preset.get("config", {})
    effective = {**cfg, **overlay_cfg,
                 "active_preset": f"{pick['preset_key']}_auto",
                 "_auto_preset_source": pick["preset_key"]}
    return effective, {
        "enabled": True,
        "selected_preset": pick["preset_key"],
        "regime_execution_mode": regime_exec,
        "regime": regime,
        "reason": pick["reason"],
    }


# ───────────────────────── orchestrator ─────────────────────

async def apply_adaptive_overlays(signal: dict, cfg: dict,
                                  user_id: str,
                                  account_id: Optional[str] = None) -> dict:
    """One-shot helper for bot_runner. Returns a dict with:
        effective_signal, effective_cfg, risk_multiplier, auto_preset_info,
        profit_taking_info.
    Order:
      1. Auto-preset overlay (Phase 3) — picks the preset persona first.
      2. Profit-taking mode (Phase 1) — clips TPs + tightens partial/trail
         using the PRESET-OVERLAID cfg.
      3. Risk multiplier (Phase 2) — computed from history, returned as
         a scalar; bot_runner applies it to the profile risk_pct.
    """
    # Phase 3
    cfg_after_preset, preset_info = apply_auto_preset(cfg, signal)

    # Phase 1
    eff_signal, eff_cfg = apply_profit_taking_mode(signal, cfg_after_preset)

    # Phase 2
    risk_info = {"multiplier": 1.0, "enabled": False}
    if cfg.get("adaptive_risk_enabled"):
        window = int(cfg.get("adaptive_risk_window") or 20)
        try:
            risk_info = await compute_risk_multiplier(user_id, account_id, window=window)
            risk_info["enabled"] = True
        except Exception as e:  # noqa: BLE001
            logger.warning("compute_risk_multiplier failed: %s — defaulting to neutral", e)
            risk_info = {"multiplier": 1.0, "enabled": True, "samples": 0,
                         "reason": f"adaptive_risk error: {type(e).__name__}"}

    return {
        "effective_signal": eff_signal,
        "effective_cfg": eff_cfg,
        "risk_multiplier": float(risk_info.get("multiplier") or 1.0),
        "risk_info": risk_info,
        "auto_preset_info": preset_info,
        "profit_taking_info": eff_signal.get("adaptive_profit_taking", {}),
    }
