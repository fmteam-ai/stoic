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
from agents.technical_agent import TechnicalAnalysisAgent
from agents.macro_agent import MacroAnalysisAgent
from agents.news_sentiment_agent import NewsSentimentAgent
from agents.strategy_agent import StrategyAgent
from agents.risk_agent import RiskAgent
from agents.portfolio_allocator_agent import PortfolioAllocatorAgent
from agents.execution_optimizer_agent import ExecutionOptimizerAgent
from agents.execution_agent import ExecutionAgent

logger = logging.getLogger("agent.orchestrator")


class Orchestrator:
    def __init__(self):
        # Legacy Research kept for callers (StrategyAgent prompt enrichment).
        self.research = ResearchAgent()
        # New specialized analysers — run in parallel before Strategy.
        self.technical = TechnicalAnalysisAgent()
        self.macro = MacroAnalysisAgent()
        self.news = NewsSentimentAgent()
        self.strategy = StrategyAgent()
        self.risk = RiskAgent()
        # New post-Risk shapers — run sequentially before Execution.
        self.allocator = PortfolioAllocatorAgent()
        self.exec_optimizer = ExecutionOptimizerAgent()
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
        account: dict | None = None,
    ) -> dict:
        """Run the full agent pipeline for a single (user, symbol) tick.

        Pipeline (parallel where independent):
          1. Technical + Macro + News  ─ parallel ─┐
          2. Strategy                    ←─────────┘
          3. Risk
          4. PortfolioAllocator   (mutates lot_size)
          5. ExecutionOptimizer   (may defer)

        Execution itself is NOT performed here — the caller (bot_runner)
        decides whether to fire based on the activity log.
        """
        tick_id = uuid.uuid4().hex
        started = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        steps: list[dict] = []

        # 1. Parallel analysers — Technical / Macro / NewsSentiment
        analyser_results = await self._run_analysers_parallel(symbol, steps)
        technical = analyser_results["technical"]
        macro = analyser_results["macro"]
        news = analyser_results["news"]

        # Compose the research payload Strategy still consumes.
        research_payload = self._compose_research(technical, macro, news)

        # 2. Strategy
        st_t0 = time.monotonic()
        try:
            signal = await self.strategy.propose(symbol, risk_level, research_payload, user_cfg=user_cfg)
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

        # 4. Portfolio Allocator — adjust lot_size when warranted
        if (signal or {}).get("action") in ("BUY", "SELL"):
            pa_t0 = time.monotonic()
            try:
                alloc = await self.allocator.allocate(
                    user_id=user_id, signal=signal, technical=technical,
                    active_positions=active_positions or [],
                )
                if alloc.get("applied"):
                    signal["original_lot_size_proposed"] = alloc["original_lot"]
                    signal["lot_size"] = alloc["adjusted_lot"]
                    signal["portfolio_allocator"] = alloc
                steps.append({
                    "agent": "portfolio_allocator",
                    "status": "ok" if alloc.get("applied") else "skipped",
                    "summary": alloc.get("bias", "no adjustment"),
                    "details": alloc,
                    "took_ms": int((time.monotonic() - pa_t0) * 1000),
                })
            except Exception as e:
                logger.exception("PortfolioAllocator failed: %s", e)
                steps.append({"agent": "portfolio_allocator", "status": "failed",
                              "error": str(e),
                              "took_ms": int((time.monotonic() - pa_t0) * 1000)})

        # 5. Execution Optimizer — may defer this tick (caller respects via `final_action`)
        if (signal or {}).get("action") in ("BUY", "SELL"):
            eo_t0 = time.monotonic()
            try:
                eo = await self.exec_optimizer.optimize(
                    signal=signal, account=account,
                )
                if not eo.get("approved"):
                    signal["execution_deferred"] = True
                    signal["execution_defer_reason"] = eo.get("deferred_reason")
                    # Convert to HOLD so downstream callers respect the defer
                    signal["action"] = "HOLD"
                    signal["tradeable"] = False
                if eo.get("slice_plan"):
                    signal["slice_plan"] = eo["slice_plan"]
                signal["execution_optimizer"] = eo
                steps.append({
                    "agent": "execution_optimizer",
                    "status": "ok" if eo.get("approved") else "vetoed",
                    "summary": eo.get("bias", ""),
                    "details": eo,
                    "took_ms": int((time.monotonic() - eo_t0) * 1000),
                })
            except Exception as e:
                logger.exception("ExecutionOptimizer failed: %s", e)
                steps.append({"agent": "execution_optimizer", "status": "failed",
                              "error": str(e),
                              "took_ms": int((time.monotonic() - eo_t0) * 1000)})

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

    async def _run_analysers_parallel(self, symbol: str, steps: list) -> dict:
        """Run TechnicalAnalysis + MacroAnalysis + NewsSentiment concurrently.

        Each emits its own activity-log entry. Failures degrade gracefully —
        a missing analyser returns {} so Strategy can still compose its prompt.
        """
        async def _run(agent, key):
            t0 = time.monotonic()
            try:
                result = await agent.analyze(symbol)
                steps.append({
                    "agent": agent.name, "status": "ok",
                    "summary": result.get("bias", "ok"),
                    "took_ms": int((time.monotonic() - t0) * 1000),
                })
                return key, result
            except Exception as e:  # noqa: BLE001
                logger.exception("%s analyser failed: %s", agent.name, e)
                steps.append({"agent": agent.name, "status": "failed",
                              "error": str(e),
                              "took_ms": int((time.monotonic() - t0) * 1000)})
                return key, {}

        results = await asyncio.gather(
            _run(self.technical, "technical"),
            _run(self.macro, "macro"),
            _run(self.news, "news"),
            return_exceptions=False,
        )
        return {k: v for k, v in results}

    def _compose_research(self, technical: dict, macro: dict, news: dict) -> dict:
        """Compose the dict that StrategyAgent forwards as `research_payload`.

        Keeps the legacy shape the prompt expects so the change is invisible
        to ai_signals.analyze_symbol.
        """
        return {
            "symbol": (technical or {}).get("symbol")
                       or (macro or {}).get("symbol")
                       or (news or {}).get("symbol"),
            "sentiment": {
                "score": (news or {}).get("score"),
                "label": (news or {}).get("label"),
                "summary": (news or {}).get("summary"),
                "article_count": (news or {}).get("article_count"),
            },
            "macro_freeze": (macro or {}).get("macro_freeze"),
            "upcoming_macro": (macro or {}).get("upcoming_events") or [],
            "cot_positioning": (macro or {}).get("cot_positioning"),
            "real_yield_10y": (macro or {}).get("real_yield_10y"),
            "dxy": (macro or {}).get("dxy"),
            "fred": (macro or {}).get("fred"),
            "macro_gate": (macro or {}).get("macro_gate"),
            "technical": technical or {},
        }


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
