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


# --- Strategy Builder -------------------------------------------------------
STRATEGY_BUILDER_SYSTEM = """You are a quantitative trading strategy compiler.
Convert a user's natural-language strategy description into STRICT JSON matching
this exact schema (do not invent extra keys):

{
  "risk_level": "low" | "medium" | "high" | "extreme",
  "symbols": ["XAUUSD" | "BTCUSD" | "EURUSD" | ...],
  "max_concurrent_trades": 1..10,        (positions open AT THE SAME TIME — "max 2 trades at once")
  "max_trades_per_day": 0..20,           (NEW entries per UTC day — "max 2 trades a day"; 0 = unlimited)
  "auto_execute": true | false,          (ONLY when the user explicitly asks to turn automatic execution on/off)
  "session_preference": "london" | "ny" | "tokyo" | "any",
  "strategy_style": "trend_following" | "mean_reversion" | "scalping" | "swing",
  "notes": "...one short sentence explaining your translation..."
}

Rules:
- Output JSON only — no prose, no markdown, no fences.
- If the prompt is ambiguous, output: {"clarification_needed": "...question..."}
- Map vague risk words: 'safe/conservative'→low, 'balanced'→medium, 'aggressive'→high, 'yolo/max'→extreme
- Default symbols to ["XAUUSD","BTCUSD"] if not specified
- Include max_concurrent_trades only when the prompt limits SIMULTANEOUS positions; include max_trades_per_day only
  when it limits trades per day ("2 trades a day" → max_trades_per_day: 2, NOT max_concurrent_trades)
- Omit auto_execute unless the user explicitly asks to enable or disable automatic execution — never default it
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


async def build_strategy(prompt: str) -> dict:
    """Convert NL strategy description → structured bot config."""
    import llm_client
    response = await llm_client.complete("analysis", STRATEGY_BUILDER_SYSTEM, prompt, feature="nl_strategy", seconds=30)
    parsed = _parse_json(str(response))
    return parsed


async def interpret_command(prompt: str) -> dict:
    """Convert NL command → structured action list."""
    import llm_client
    response = await llm_client.complete("analysis", COMMAND_INTERPRETER_SYSTEM, prompt, feature="nl_command", seconds=30)
    parsed = _parse_json(str(response))
    return parsed
