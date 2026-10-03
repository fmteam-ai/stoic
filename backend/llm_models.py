"""Single source of truth for LLM model selection (roadmap step 7).

Every AI call site asks for a TIER, never a model string:

    from llm_models import model_for, PROVIDER
    chat = LlmChat(...).with_model(PROVIDER, model_for("analysis"))

Tiers
-----
fast        cheap / high-volume: news tagging, Fed tone, journal cards
analysis    default reasoning: copilot, insights, AI signals, loss advisor, bot doctor, NL commander
deep        long-form generation: strategy code, research hypotheses
ai_optimizer / ai_optimizer_fallback   strategy optimizer (tries primary, falls back)

Override any tier with an env var (read at call time, no restart needed in-process):
    LLM_MODEL_FAST, LLM_MODEL_ANALYSIS, LLM_MODEL_DEEP,
    LLM_MODEL_AI_OPTIMIZER, LLM_MODEL_AI_OPTIMIZER_FALLBACK

`tests/unit/test_llm_models_single_source.py` fails CI if any other file
hard-codes a model identifier again.
"""
from __future__ import annotations

import os

PROVIDER = "anthropic"

# Identifiers confirmed against the Emergent Universal Key model list (2026-10-02).
DEFAULTS: dict[str, str] = {
    "fast": "claude-haiku-4-5-20251001",
    "analysis": "claude-sonnet-5-5",
    "deep": "claude-opus-5-5",
    "ai_optimizer": "claude-fable-5-1",
    "ai_optimizer_fallback": "claude-opus-5-5",
}

ENV_VARS: dict[str, str] = {tier: f"LLM_MODEL_{tier.upper()}" for tier in DEFAULTS}

TIERS = tuple(DEFAULTS)


def model_for(tier: str) -> str:
    """Model identifier for `tier`, honouring the env override when set."""
    if tier not in DEFAULTS:
        raise KeyError(f"unknown LLM tier {tier!r}; valid: {', '.join(TIERS)}")
    override = (os.environ.get(ENV_VARS[tier]) or "").strip()
    return override or DEFAULTS[tier]


def optimizer_candidates() -> list[tuple[str, str]]:
    """(provider, model) attempts for the strategy optimizer — primary then fallback."""
    primary, fallback = model_for("ai_optimizer"), model_for("ai_optimizer_fallback")
    out = [(PROVIDER, primary)]
    if fallback != primary:
        out.append((PROVIDER, fallback))
    return out


def describe() -> dict:
    """Resolved tier → model map + which came from env (for Bot Health diagnostics)."""
    return {
        "provider": PROVIDER,
        "tiers": {
            tier: {"model": model_for(tier), "default": DEFAULTS[tier],
                   "env_var": ENV_VARS[tier],
                   "overridden": bool((os.environ.get(ENV_VARS[tier]) or "").strip())}
            for tier in TIERS
        },
    }
