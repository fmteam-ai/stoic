"""Orchestrator — coordinates the four agents on every tick and persists
the activity log to MongoDB (`agent_activity` collection).

Activity-log shape (one document per tick per (user, symbol)):
  {
    "user_id": str,
    "symbol": str,
    "tick_id": uuid,
    "started_at": iso,
    "completed_at": iso,
    "duration_ms": int,
    "steps": [
       {"agent": "research", "status": "ok", "summary": "...", "took_ms": int},
       {"agent": "strategy", "status": "ok", "summary": "...", "took_ms": int},
       {"agent": "risk",     "status": "ok"|"vetoed", "overrides": [...], "took_ms": int},
       {"agent": "execution","status": "ok"|"skipped"|"failed", "trade_id": str|None}
    ],
    "final_action": "BUY|SELL|HOLD",
    "final_confidence": float,
  }
"""
import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone

from database import get_db
from agents.research_agent import ResearchAgent
from agents.strategy_agent import StrategyAgent
from agents.risk_agent import RiskAgent
from agents.execution_agent import ExecutionAgent

logger = logging.getLogger("agent.orchestrator")


class Orchestrator:
    def __init__(self):
        self.research = ResearchAgent()
        self.strategy = StrategyAgent()
        self.risk = RiskAgent()
        self.execution = ExecutionAgent()

    async def _log(self, doc: dict) -> None:
        try:
            db = get_db()
            await db.agent_activity.insert_one(doc)
        except Exception as e:
            logger.warning("Failed to persist agent_activity: %s", e)

    async def analyze_tick(
        self,
        *,
        user_id: str,
        symbol: str,
        risk_level: str,
        active_positions: list[dict] | None = None,
        user_cfg: dict | None = None,
    ) -> dict:
        """Run Research → Strategy → Risk for a single (user, symbol) tick.

        Returns the final post-risk signal dict plus an activity-log doc id.
        Execution is NOT performed here — the caller (bot_runner) handles
        spread filter, auto-tune block, account selection.
        """
        tick_id = uuid.uuid4().hex
        started = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        steps: list[dict] = []

        # 1. Research
        ra_t0 = time.monotonic()
        try:
            research = await self.research.gather(symbol)
            steps.append({
                "agent": "research", "status": "ok",
                "summary": _summarise_research(research),
                "took_ms": int((time.monotonic() - ra_t0) * 1000),
            })
        except Exception as e:
            logger.exception("ResearchAgent failed: %s", e)
            research = {}
            steps.append({"agent": "research", "status": "failed", "error": str(e),
                          "took_ms": int((time.monotonic() - ra_t0) * 1000)})

        # 2. Strategy
        st_t0 = time.monotonic()
        try:
            signal = await self.strategy.propose(symbol, risk_level, research, user_cfg=user_cfg)
            steps.append({
                "agent": "strategy", "status": "ok",
                "summary": f"{signal.get('action')} {signal.get('confidence')}%",
                "took_ms": int((time.monotonic() - st_t0) * 1000),
            })
        except Exception as e:
            logger.exception("StrategyAgent failed: %s", e)
            steps.append({"agent": "strategy", "status": "failed", "error": str(e),
                          "took_ms": int((time.monotonic() - st_t0) * 1000)})
            duration_ms = int((time.monotonic() - t0) * 1000)
            await self._log({
                "user_id": user_id, "symbol": symbol, "tick_id": tick_id,
                "started_at": started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": duration_ms, "steps": steps,
                "final_action": "HOLD", "final_confidence": 0,
            })
            return {"signal": None, "tick_id": tick_id, "activity": steps}

        # 3. Risk
        rk_t0 = time.monotonic()
        try:
            review = await self.risk.review(symbol, signal, active_positions or [])
            signal = review["signal"]
            risk_status = "vetoed" if review["overrides"] else "ok"
            steps.append({
                "agent": "risk", "status": risk_status,
                "summary": (review["overrides"][0]["reason"] if review["overrides"]
                            else f"approved {signal.get('action')}"),
                "overrides": review["overrides"],
                "took_ms": int((time.monotonic() - rk_t0) * 1000),
            })
        except Exception as e:
            logger.exception("RiskAgent failed: %s", e)
            steps.append({"agent": "risk", "status": "failed", "error": str(e),
                          "took_ms": int((time.monotonic() - rk_t0) * 1000)})

        duration_ms = int((time.monotonic() - t0) * 1000)
        await self._log({
            "user_id": user_id,
            "symbol": symbol,
            "tick_id": tick_id,
            "started_at": started,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": duration_ms,
            "steps": steps,
            "final_action": signal.get("action") if signal else "HOLD",
            "final_confidence": signal.get("confidence") if signal else 0,
        })
        return {"signal": signal, "tick_id": tick_id, "activity": steps}


def _summarise_research(r: dict) -> str:
    """One-line digest of the research payload for the activity log."""
    parts: list[str] = []
    s = (r.get("sentiment") or {}).get("score")
    if s is not None:
        parts.append(f"news {s:+.2f}")
    fred = r.get("fred") or {}
    fred_series = (fred.get("series") or {})
    if "VIXCLS" in fred_series:
        parts.append(f"VIX {fred_series['VIXCLS']['value']:.1f}")
    if "DGS10" in fred_series:
        parts.append(f"10Y {fred_series['DGS10']['value']:.2f}%")
    cot = r.get("cot_positioning") or {}
    if cot.get("overcrowded_long"):
        parts.append("COT-OC-LONG")
    if cot.get("overcrowded_short"):
        parts.append("COT-OC-SHORT")
    dxy = r.get("dxy") or {}
    if dxy.get("regime"):
        parts.append(f"DXY {dxy['regime']}")
    return " · ".join(parts) if parts else "ok"


# Module-level singleton for callers that want a simple import
_default: Orchestrator | None = None


def get_orchestrator() -> Orchestrator:
    global _default
    if _default is None:
        _default = Orchestrator()
    return _default
