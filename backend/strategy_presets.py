"""
Strategy Presets — named bot personas that overlay onto a user's bot_config.

Each preset is a partial dict applied via POST /api/bot/preset/{key}.
The preset only changes the listed fields; everything else (symbols, custom
spread caps, drawdown caps) stays as the user already configured it.

Design philosophy:
    - Presets only tune *behaviour* knobs (confidence floor, trade frequency,
      protective toggles). They never touch:
        • risk_level         → that's a separate user choice
        • symbols            → user-chosen
        • per-symbol caps    → user-tuned
        • drawdown limits    → user-chosen safety net
"""

PRESETS = {
    "sniper": {
        "label": "Sniper",
        "tagline": "Patient. Precise. A-grade only.",
        "description": (
            "ENGINE: Strict MTF Cascade (4H→1H→15M pullback→breakout). Low frequency, high conviction. Waits for confluence + macro alignment "
            "and only fires when confidence is well above the profile floor. Trail "
            "is loose so winners run."
        ),
        "icon": "Crosshair",
        "color": "#FFD700",
        "config": {
            "min_confidence_override": 75,
            "trade_of_day_cap": 1,
            "max_concurrent_trades": 2,
            "trailing_enabled": True,
            "trailing_start_r": 2.0,
            "trailing_distance_r": 1.0,
            "partial_close_enabled": True,
            "partial_close_trigger_r": 1.5,
            "partial_close_fraction": 0.4,
            "sl_cooldown_enabled": True,
            "sl_cooldown_minutes": 90,
        },
    },
    "scalper": {
        "label": "Scalper",
        "tagline": "Fast in, fast out, many small wins.",
        "description": (
            "ENGINE: HF Momentum Scalper (M15 bursts + VWAP bounces, 0.25% risk/trade, 5-min re-entry). High-tempo execution. Lower confidence floor, more concurrent positions, "
            "tight trailing for quick lock-ins. Best for active sessions on tight spreads."
        ),
        "icon": "Zap",
        "color": "#00FF41",
        "config": {
            "signal_cooldown_minutes": 5,
            "min_confidence_override": 55,
            "trade_of_day_cap": 6,
            "max_concurrent_trades": 4,
            "trailing_enabled": True,
            "trailing_start_r": 0.8,
            "trailing_distance_r": 0.4,
            "partial_close_enabled": True,
            "partial_close_trigger_r": 0.8,
            "partial_close_fraction": 0.5,
            "sl_cooldown_enabled": False,
            "sl_cooldown_minutes": 15,
        },
    },
    "fast_scalp": {
        "label": "Fast Scalp",
        "tagline": "Quick in, ≤100-pip out — pure win-rate hunter.",
        "description": (
            "ENGINE: HF Momentum Scalper turbo (softer thresholds, 3-min re-entry, 0.25% risk/trade). Maximises the number of green trades. Locks 70% off at 0.5R, "
            "trails the runner tight, and hard-caps profit at 100 pips per "
            "trade. Pair with profit_taking_mode='win_rate' for the full effect."
        ),
        "icon": "Zap",
        "color": "#10F2C5",
        "config": {
            "signal_cooldown_minutes": 3,
            "min_confidence_override": 60,
            "trade_of_day_cap": 8,
            "max_concurrent_trades": 4,
            "trailing_enabled": True,
            "trailing_start_r": 0.5,
            "trailing_distance_r": 0.25,
            "partial_close_enabled": True,
            "partial_close_trigger_r": 0.5,
            "partial_close_fraction": 0.7,
            "breakeven_enabled": True,
            "breakeven_trigger_r": 0.4,
            "sl_cooldown_enabled": False,
            "profit_taking_mode": "win_rate",
            "max_tp_pips_per_symbol": {"XAUUSD": 100, "BTCUSD": 100},
        },
    },
    "trend_rider": {
        "label": "Trend Rider",
        "tagline": "Catch the wave, hold the line.",
        "description": (
            "ENGINE: Relaxed MTF Cascade (1H boss, 4H non-opposing, wide pullback window). Built for sustained directional moves. Wide trailing windows to let "
            "winners stretch, large partial-close R-multiples, news-protector ON."
        ),
        "icon": "TrendingUp",
        "color": "#0099FF",
        "config": {
            "min_confidence_override": 68,
            "trade_of_day_cap": 2,
            "max_concurrent_trades": 3,
            "trailing_enabled": True,
            "trailing_start_r": 1.5,
            "trailing_distance_r": 1.2,
            "partial_close_enabled": True,
            "partial_close_trigger_r": 1.5,
            "partial_close_fraction": 0.3,
            "pre_news_protect_enabled": True,
            "pre_news_protect_minutes": 5,
        },
    },
    "breakout": {
        "label": "Breakout Hunter",
        "tagline": "Wait for volatility expansion.",
        "description": (
            "ENGINE: Donchian-20 M15 breakout with momentum confirmation. Only fires when volatility breaks out of its recent range. Strong anti-tilt "
            "settings prevent churn during chop. Skips Asia-session XAU by default."
        ),
        "icon": "Rocket",
        "color": "#FF6B00",
        "config": {
            "breakeven_trigger_r": 0.6,
            "min_confidence_override": 70,
            "trade_of_day_cap": 2,
            "max_concurrent_trades": 3,
            "asia_session_skip_xau": True,
            "anti_tilt_enabled": True,
            "anti_tilt_consecutive_losses": 2,
            "anti_tilt_freeze_hours": 6,
            "trailing_enabled": True,
            "trailing_start_r": 1.5,
            "trailing_distance_r": 0.8,
            "pre_news_protect_enabled": True,
        },
    },
    "mean_reversion": {
        "label": "Mean Reversion",
        "tagline": "Buy weakness, sell strength.",
        "description": (
            "ENGINE: Range Fade (fade session extremes toward VWAP in confirmed ranges). Targets calm, range-bound markets. Tighter take-profits, faster partial closes. "
            "Skips trending sessions automatically via the MTF gate."
        ),
        "icon": "Activity",
        "color": "#9B59B6",
        "config": {
            "breakeven_trigger_r": 0.5,
            "min_confidence_override": 65,
            "trade_of_day_cap": 3,
            "max_concurrent_trades": 3,
            "trailing_enabled": False,
            "partial_close_enabled": True,
            "partial_close_trigger_r": 0.8,
            "partial_close_fraction": 0.6,
            "sl_cooldown_enabled": True,
            "sl_cooldown_minutes": 30,
        },
    },
    "balanced": {
        "label": "Balanced (Default)",
        "tagline": "STOIC's house defaults.",
        "description": (
            "ENGINE: Moderate MTF Cascade (1H-led, 4H non-opposing). The standard STOIC configuration. Sensible trade caps, balanced trailing, "
            "all guards on. A solid baseline before you customise."
        ),
        "icon": "Scale",
        "color": "#A1A1AA",
        "config": {
            "min_confidence_override": 0,
            "trade_of_day_cap": 1,
            "max_concurrent_trades": 3,
            "trailing_enabled": True,
            "trailing_start_r": 1.5,
            "trailing_distance_r": 0.7,
            "partial_close_enabled": True,
            "partial_close_trigger_r": 1.0,
            "partial_close_fraction": 0.5,
            "sl_cooldown_enabled": True,
            "sl_cooldown_minutes": 45,
            "pre_news_protect_enabled": True,
            "pre_news_protect_minutes": 5,
            "anti_tilt_enabled": True,
        },
    },
}


def get_preset(key: str) -> dict | None:
    return PRESETS.get(key)


def list_presets() -> list[dict]:
    """Return presets as a JSON-friendly array, ordered."""
    order = ["sniper", "scalper", "fast_scalp", "trend_rider", "breakout", "mean_reversion", "balanced"]
    out = []
    for k in order:
        if k in PRESETS:
            p = PRESETS[k]
            out.append({"key": k, **p})
    return out
