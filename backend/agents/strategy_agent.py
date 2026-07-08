"""StrategyAgent — fuses indicators + research into a candidate signal.

This is a thin orchestration wrapper around the existing
`ai_signals.analyze_symbol` (Claude Sonnet 4.5 + Logistic Regression +
10-layer veto cascade). The wrapper keeps the existing veto pipeline intact —
its only job is to standardise the entry/exit interface so the RiskAgent and
Orchestrator can compose around it.
"""
import logging

from ai_signals import analyze_symbol

logger = logging.getLogger("agent.strategy")


class StrategyAgent:
    name = "strategy"

    async def propose(self, symbol: str, risk_level: str,
                      research_payload: dict | None = None,
                      user_cfg: dict | None = None) -> dict:
        """Produce a candidate signal for `symbol` under `risk_level`.

        `research_payload` is informational — `ai_signals.analyze_symbol`
        currently fetches its own data, so we pass research through for
        observability (the orchestrator records it on the activity log).
        `user_cfg` lets per-user overrides flow into the analyzer (aggressive
        mode, custom min-confidence threshold).
        """
        cfg = user_cfg or {}
        signal = await analyze_symbol(
            symbol, risk_level,
            min_conf_override=int(cfg.get("min_confidence_override") or 0),
            aggressive_mode=bool(cfg.get("aggressive_mode") or False),
            range_scalp_mode=bool(cfg.get("range_scalp_enabled") or False),
            mtf_confluence_mode=bool(cfg.get("mtf_confluence_enabled") or False),
        )
        return signal
