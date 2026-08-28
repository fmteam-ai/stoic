"""Nitro eligibility configuration — initial thresholds, to be
empirically calibrated once live evidence accumulates (never treat
these numbers as final)."""

ENABLED_MIN = 90
REDUCED_MIN = 80

# component weights (sum 1.0) — execution factors dominate by design
WEIGHTS = {
    "market_quality": 0.10,
    "liquidity": 0.12,
    "spread_quality": 0.16,
    "broker_quality": 0.14,
    "latency_quality": 0.16,
    "slippage_quality": 0.12,
    "infrastructure_health": 0.12,
    "regime_compatibility": 0.08,
}

# any HARD component at/below this floor pauses Nitro regardless of the
# weighted score — one broken execution leg is disqualifying
HARD_FLOOR = 50
HARD_COMPONENTS = ("spread_quality", "latency_quality",
                   "infrastructure_health", "broker_quality")
