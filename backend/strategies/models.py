"""Strategy definition model — the single shape every registered strategy
must satisfy. Definitions are DATA: adding a fifth strategy means adding
a registry entry, never rewriting PAMM."""
from pydantic import BaseModel


class StrategyDefinition(BaseModel):
    strategy_id: str
    display_name: str
    family: str
    version: str
    enabled: bool = True
    pamm_eligible: bool = False
    requires_broker_certification: bool = False
    requires_latency_certification: bool = False
    # deterministic MT5 magic namespace (advisory identity only — the
    # authoritative identity is ALWAYS execution_intent_id)
    magic_code: int
    # genuinely different decision/execution characteristics
    characteristics: dict
    # ordered strategy-specific gates the pipeline must pass
    gates: list
