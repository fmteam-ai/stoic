"""Central Strategy Registry (v62.1) — SNIPER · SCALPER · FAST_SCALP ·
NITRO_SCALPER. These are genuinely different strategies (different
selectivity, holding time, execution sensitivity and gate pipelines),
NOT aliases for risk levels. PAMM never hard-codes strategy names — it
asks this registry."""
import hashlib
import json

from strategies.models import StrategyDefinition

MAGIC_NAMESPACE = 62000  # STOIC namespace + strategy code

_DEFS = [
    StrategyDefinition(
        strategy_id="sniper", display_name="Sniper", family="selective",
        version="1.0.0", enabled=True, pamm_eligible=True,
        requires_broker_certification=True,
        requires_latency_certification=False,
        magic_code=MAGIC_NAMESPACE + 1,
        characteristics={
            "frequency": "LOW", "selectivity": "VERY_HIGH",
            "holding_time": "MEDIUM", "latency_sensitivity": "MEDIUM",
            "spread_sensitivity": "MEDIUM",
            "confidence_threshold": "VERY_HIGH"},
        gates=["opportunity", "regime", "market_memory", "uncertainty",
               "meta_decision", "sniper_quality_gate", "portfolio_brain",
               "pretrade_twin", "risk", "execution"]),
    StrategyDefinition(
        strategy_id="scalper", display_name="Scalper", family="scalping",
        version="1.0.0", enabled=True, pamm_eligible=True,
        requires_broker_certification=True,
        requires_latency_certification=True,
        magic_code=MAGIC_NAMESPACE + 2,
        characteristics={
            "frequency": "MEDIUM_HIGH", "selectivity": "HIGH",
            "holding_time": "SHORT", "latency_sensitivity": "HIGH",
            "spread_sensitivity": "HIGH",
            "confidence_threshold": "HIGH",
            "execution_inputs": ["spread", "slippage", "liquidity",
                                 "broker_quality", "execution_latency"]},
        gates=["opportunity", "regime", "spread_gate", "broker_quality",
               "uncertainty", "meta_decision", "portfolio_brain",
               "pretrade_twin", "risk", "execution"]),
    StrategyDefinition(
        strategy_id="fast_scalp", display_name="Fast Scalp",
        family="scalping", version="1.0.0", enabled=True,
        pamm_eligible=True, requires_broker_certification=True,
        requires_latency_certification=True,
        magic_code=MAGIC_NAMESPACE + 3,
        characteristics={
            "frequency": "HIGH", "selectivity": "HIGH",
            "holding_time": "VERY_SHORT",
            "latency_sensitivity": "VERY_HIGH",
            "spread_sensitivity": "VERY_HIGH",
            "confidence_threshold": "HIGH",
            "eligibility_requires": [
                "broker_healthy", "spread_acceptable",
                "latency_acceptable", "slippage_acceptable",
                "position_truth_healthy", "clock_healthy",
                "execution_infra_healthy"],
            "on_condition_failure": "SKIP"},
        gates=["fast_eligibility", "regime", "spread_gate", "latency_gate",
               "uncertainty", "meta_decision", "portfolio_brain",
               "fast_twin", "risk", "execution"]),
    StrategyDefinition(
        strategy_id="nitro_scalper", display_name="Nitro Scalper",
        family="scalping", version="1.0.0", enabled=True,
        pamm_eligible=True, requires_broker_certification=True,
        requires_latency_certification=True,
        magic_code=MAGIC_NAMESPACE + 4,
        characteristics={
            "frequency": "OPPORTUNISTIC", "selectivity": "MAXIMUM",
            "holding_time": "EXTREMELY_SHORT",
            "latency_sensitivity": "MAXIMUM",
            "spread_sensitivity": "MAXIMUM",
            "confidence_threshold": "MAXIMUM",
            "meaning": "maximum EXECUTION SELECTIVITY for extremely "
                       "short-lived opportunities — NEVER maximum risk",
            "on_condition_failure": "NO_NITRO"},
        gates=["nitro_eligibility", "regime", "liquidity", "spread_gate",
               "broker_intelligence", "latency_gate", "clock_health",
               "slippage_gate", "uncertainty", "execution_alpha",
               "fast_twin", "pamm_risk", "execution_authority"]),
]

REGISTRY: dict[str, StrategyDefinition] = {d.strategy_id: d for d in _DEFS}


def get_strategy(strategy_id: str) -> StrategyDefinition | None:
    return REGISTRY.get(str(strategy_id or "").lower())


def pamm_eligible_strategies() -> list[StrategyDefinition]:
    return [d for d in REGISTRY.values() if d.enabled and d.pamm_eligible]


def strategy_hash(defn: StrategyDefinition) -> str:
    """Deterministic artifact/config hash — pins exactly what a version
    means so an innocent update can never silently change live behavior."""
    canonical = json.dumps(defn.model_dump(), sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]
