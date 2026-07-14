"""iter-134 · Version stamps for auditability (quant roadmap #6).

Every decision and executed trade records exactly which code generated it,
so performance changes can be attributed to model / risk / execution changes.
Bump these when the corresponding subsystem changes behavior.
"""

RISK_POLICY_VERSION = "risk_v133"        # fixed-fraction default, fail-closed pipeline
EXECUTION_POLICY_VERSION = "exec_v129"   # session/exhaustion gates + trend-ride
FEATURE_SCHEMA_VERSION = "fs_2026_07"    # intraday_features M15 pack
DECISION_LEDGER_VERSION = "ledger_v1"

STRATEGY_VERSIONS = {
    "hf_scalp": "hf_scalp_v132",         # + trend-day continuation entries
    "hf_scalp_fast": "hf_scalp_fast_v132",
    "range_fade": "range_fade_v128",     # + knife filter
    "breakout_m15": "breakout_v127",
    "mtf": "mtf_v126",
}


def version_stamp(scope: str | None = None) -> dict:
    return {
        "strategy_version": STRATEGY_VERSIONS.get(
            scope or "", scope or "unknown"),
        "risk_policy": RISK_POLICY_VERSION,
        "execution_policy": EXECUTION_POLICY_VERSION,
        "feature_schema": FEATURE_SCHEMA_VERSION,
    }
