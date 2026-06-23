"""STOIC Multi-Agent Architecture — top-level package.

Four named agents collaborate on every tick, orchestrated by a coordinator:

  1. ResearchAgent  → gathers unstructured & macro signals (news, COT, TIPS, DXY, FRED).
  2. StrategyAgent  → fuses indicators + research into a candidate signal via Claude + LR.
  3. RiskAgent      → cross-asset correlation + portfolio guards; can veto Strategy.
  4. ExecutionAgent → forwards approved orders to the MT5 bridge / paper engine.

Each agent emits a structured activity log entry per tick (stored in
`agent_activity` collection) so users can see the full decision chain on the
`/agents` page in the UI.

The agents are **additive wrappers** around the existing modules — they do not
replace `ai_signals.analyze_symbol`, `execution.py`, etc. Instead, they
package the existing pipeline behind a clean contract, add the new alpha
(FRED + correlation), and persist the audit trail.
"""
from agents.research_agent import ResearchAgent
from agents.strategy_agent import StrategyAgent
from agents.risk_agent import RiskAgent
from agents.execution_agent import ExecutionAgent
from agents.orchestrator import Orchestrator

__all__ = [
    "ResearchAgent", "StrategyAgent", "RiskAgent", "ExecutionAgent", "Orchestrator",
]
