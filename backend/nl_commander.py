"""Natural-Language Command Hub.

Two LLM-powered surfaces over the bot:

  1. build_strategy(prompt)     — convert "scalp gold during London session" into
                                   a fully-formed bot_config JSON.
  2. interpret_command(prompt)  — convert "if BTC drops 3% disable my high-risk
                                   bots and move gold stops to break-even" into
                                   a list of structured actions executed on the
                                   running bot thread.

Backed by Claude Sonnet 4.5 via the Emergent LLM Key. Strict JSON only; any
ambiguity returns a clarification request instead of inventing parameters.
"""
import json
import re
from typing import Optional

from pydantic import BaseModel, Field

import llm_client


# --- Strategy Builder -------------------------------------------------------
STRATEGY_BUILDER_SYSTEM = """You are a quantitative trading strategy compiler.
Convert a user's natural-language strategy description into STRICT JSON matching
this exact schema (do not invent extra keys):

{
  "risk_level": "low" | "medium" | "high" | "extreme",
  "symbols": ["XAUUSD" | "BTCUSD" | "EURUSD" | ...],
  "max_concurrent_trades": 1..10,
  "auto_execute": true | false,
  "session_preference": "london" | "ny" | "tokyo" | "any",
  "strategy_style": "trend_following" | "mean_reversion" | "scalping" | "swing",
  "notes": "...one short sentence explaining your translation..."
}

Rules:
- Output JSON only — no prose, no markdown, no fences.
- If the prompt is ambiguous, output: {"clarification_needed": "...question..."}
- Map vague risk words: 'safe/conservative'→low, 'balanced'→medium, 'aggressive'→high, 'yolo/max'→extreme
- Default symbols to ["XAUUSD","BTCUSD"] if not specified
- Always include max_concurrent_trades (default 2)
"""


COMMAND_INTERPRETER_SYSTEM = """You are an in-app trading risk commander.
You translate a user's natural-language command into a list of structured
actions the backend can execute. Output STRICT JSON only.

Schema:
{
  "actions": [
    {
      "type": "DISABLE_BOTS" | "ENABLE_BOTS" | "MOVE_STOPS_BREAKEVEN" |
              "CLOSE_ALL_TRADES" | "SET_RISK_LEVEL" | "PANIC_LOCK" |
              "SET_CONDITIONAL_TRIGGER",
      "target": "all" | "high_risk" | "<symbol>" | "<account_label>",
      "params": {...action-specific...}
    }
  ],
  "summary": "1-line human-readable confirmation of what will happen"
}

Conditional triggers:
  - For phrases like "if BTC drops 3%, disable high-risk bots":
    type=SET_CONDITIONAL_TRIGGER,
    params={"symbol":"BTCUSD","condition":"drop","threshold_pct":3.0,
            "then":[{"type":"DISABLE_BOTS","target":"high_risk"}]}

Rules:
- If ambiguous, output: {"clarification_needed":"...question..."}
- Never invent actions outside the enumerated `type`s above.
- Stops to break-even uses MOVE_STOPS_BREAKEVEN with target=symbol
"""


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except Exception as e:
        return {"error": f"Failed to parse: {e}", "raw": text[:300]}


# --- Structured-output schemas --------------------------------------------
# Deliberately permissive (plain str / Optional): closed-vocab enforcement
# stays where it was — routes/nl_routes.py (nl_actions.validate_actions) and
# the strategy pipeline validators — so their error messages are unchanged.
class StrategyOut(BaseModel):
    clarification_needed: Optional[str] = None
    risk_level: Optional[str] = None
    symbols: Optional[list[str]] = None
    max_concurrent_trades: Optional[int] = None
    auto_execute: Optional[bool] = None
    session_preference: Optional[str] = None
    strategy_style: Optional[str] = None
    notes: Optional[str] = None


class LeafParams(BaseModel):
    risk_level: Optional[str] = None


class LeafActionOut(BaseModel):
    type: str
    target: Optional[str] = None
    params: LeafParams = Field(default_factory=LeafParams)


class ActionParams(BaseModel):
    """Union of every action's params (SET_RISK_LEVEL / SET_CONDITIONAL_TRIGGER);
    unused keys are omitted from the returned dict."""
    risk_level: Optional[str] = None
    symbol: Optional[str] = None
    condition: Optional[str] = None
    threshold_pct: Optional[float] = None
    then: Optional[list[LeafActionOut]] = None


class ActionOut(BaseModel):
    type: str
    target: Optional[str] = None
    params: ActionParams = Field(default_factory=ActionParams)


class CommandOut(BaseModel):
    clarification_needed: Optional[str] = None
    actions: list[ActionOut] = Field(default_factory=list)
    summary: Optional[str] = None


async def build_strategy(prompt: str) -> dict:
    """Convert NL strategy description → structured bot config."""
    res = await llm_client.complete(
        feature="nl_commander", system=STRATEGY_BUILDER_SYSTEM, user=prompt,
        schema=StrategyOut, max_tokens=800)
    if not res.ok:
        return {"error": f"Failed to parse: {res.error}", "raw": (res.text or "")[:300]}
    return res.data.model_dump(exclude_none=True)


async def interpret_command(prompt: str) -> dict:
    """Convert NL command → structured action list."""
    res = await llm_client.complete(
        feature="nl_commander", system=COMMAND_INTERPRETER_SYSTEM, user=prompt,
        schema=CommandOut, max_tokens=1200)
    if not res.ok:
        return {"error": f"Failed to parse: {res.error}", "raw": (res.text or "")[:300]}
    return res.data.model_dump(exclude_none=True)
