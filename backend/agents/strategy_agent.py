"""StrategyAgent — fuses indicators + research into a candidate signal.

This is a thin orchestration wrapper around the existing
`ai_signals.analyze_symbol` (Claude Sonnet 4.5 + Logistic Regression +
10-layer veto cascade). The wrapper keeps the existing veto pipeline intact —
its only job is to standardise the entry/exit interface so the RiskAgent and
Orchestrator can compose around it.
"""
import logging
import random

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
        `user_cfg` lets per-user overrides flow into the analyzer
        (custom min-confidence threshold).
        """
        cfg = user_cfg or {}
        # Canary ladder — per-signal probability draw: a running canary's
        # challenger params apply to allocation_pct% of new signals, and
        # the signal is tagged so its trades score the canary stage.
        eng_params = dict(cfg.get("engine_params") or {})
        canary_used = None
        for _engine, c in (cfg.get("engine_params_canary") or {}).items():
            try:
                pct = float(c.get("allocation_pct") or 0)
            except (TypeError, ValueError):
                continue
            if pct > 0 and random.random() * 100 < pct:
                eng_params[_engine] = c.get("params") or {}
                canary_used = {"engine": _engine,
                               "model_id": c.get("model_id"),
                               "version": c.get("version"),
                               "allocation_pct": pct}
        signal = await analyze_symbol(
            symbol, risk_level,
            min_conf_override=int(cfg.get("min_confidence_override") or 0),
            strategy=cfg.get("active_preset"),
            engine_params=eng_params,
            user_id=cfg.get("user_id"),
        )
        if canary_used and isinstance(signal, dict):
            signal["canary"] = canary_used
        return signal
