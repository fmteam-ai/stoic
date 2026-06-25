"""MacroAnalysisAgent — consolidated macroeconomic context for one symbol.

Pulls every macro stream STOIC tracks and projects them into a single
agent verdict. For XAUUSD it also consults `macro_gate` so the activity
log surfaces whether the deterministic regime gate is currently blocking
gold trades.

Output:
  {
    "symbol": str,
    "macro_freeze": dict,         # imminent event freeze (if any)
    "upcoming_events": list[dict],
    "fred": dict | None,          # FRED snapshot (5 series)
    "real_yield_10y": dict | None,
    "dxy": dict | None,
    "cot_positioning": dict | None,
    "macro_gate": dict | None,    # XAUUSD-only deterministic gate state
    "bias": str,                  # one-line digest for the activity log
  }
"""
import logging

from economic_calendar import macro_freeze_check, upcoming_for
from macro.cot import get_gold_positioning
from macro.tips import get_real_yield
from macro.dxy import get_dxy_snapshot
from macro.fred import get_macro_snapshot as get_fred_snapshot
from macro_gate import gate_status as macro_gate_status

logger = logging.getLogger("agent.macro")


class MacroAnalysisAgent:
    name = "macro"

    async def analyze(self, symbol: str) -> dict:
        sym = symbol.upper()
        macro_freeze = await macro_freeze_check(sym)
        upcoming = await upcoming_for(sym, hours=24)

        cot = tips = dxy = None
        if sym == "XAUUSD":
            try:
                cot = await get_gold_positioning()
            except Exception as e:  # noqa: BLE001
                logger.debug("COT fetch failed: %s", e)
            try:
                tips = await get_real_yield()
            except Exception as e:  # noqa: BLE001
                logger.debug("TIPS fetch failed: %s", e)
            try:
                dxy = await get_dxy_snapshot()
            except Exception as e:  # noqa: BLE001
                logger.debug("DXY fetch failed: %s", e)

        fred = None
        try:
            fred = await get_fred_snapshot()
        except Exception as e:  # noqa: BLE001
            logger.debug("FRED fetch failed: %s", e)

        # Macro gate is XAUUSD-only — non-gold returns ok=True with a stamped row
        gate = None
        try:
            gate = await macro_gate_status() if sym == "XAUUSD" else None
        except Exception as e:  # noqa: BLE001
            logger.debug("macro_gate status failed: %s", e)

        # Concise bias line
        parts: list[str] = []
        if macro_freeze and macro_freeze.get("frozen"):
            parts.append("FREEZE")
        if gate and (not gate.get("buy_ok") or not gate.get("sell_ok")):
            blocked = ", ".join(b["action"] for b in gate.get("blocked", []))
            parts.append(f"gate blocks {blocked}")
        if tips and tips.get("regime"):
            parts.append(f"real-yld {tips['regime']}")
        if dxy and dxy.get("regime"):
            parts.append(f"DXY {dxy['regime']}")
        if cot and cot.get("overcrowded_long"):
            parts.append("COT-OC-LONG")
        if cot and cot.get("overcrowded_short"):
            parts.append("COT-OC-SHORT")
        if fred and isinstance(fred, dict):
            series_iter = (
                fred.get("series").values() if isinstance(fred.get("series"), dict)
                else fred.get("series") or []
            )
            for s in series_iter:
                if isinstance(s, dict) and s.get("series_id") == "DGS10":
                    parts.append(f"10Y {s.get('latest'):.2f}%")
                    break

        return {
            "symbol": sym,
            "macro_freeze": macro_freeze,
            "upcoming_events": upcoming[:5],
            "fred": fred,
            "real_yield_10y": tips,
            "dxy": dxy,
            "cot_positioning": cot,
            "macro_gate": gate,
            "bias": " · ".join(parts) if parts else "neutral",
        }
