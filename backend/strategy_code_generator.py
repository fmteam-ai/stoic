"""Strategy Code Generator — Claude-powered DSL writer.

Takes a compiled strategy preset and produces a more granular executable
ruleset (entry/exit conditions, parameter knobs). The output is both:
  - a strict-JSON DSL the bot CAN execute against indicator snapshots
  - a `pseudocode` block (Python-flavoured) the UI renders for the wow factor

We deliberately do NOT execute arbitrary user code — the DSL is a fixed-vocab
condition tree (operators ∈ {>, <, between, in, equals}; fields ∈ a closed
set of indicator names). The bot interprets the tree; nothing eval()s.

Schema returned:
{
  "version": "1.0",
  "name": str,
  "symbols": ["XAUUSD", ...],
  "session_preference": "london"|"ny"|"tokyo"|"any",
  "params": {                       # tunable knobs the optimizer can grid-search
    "min_confidence": 70,
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "atr_pct_max": 1.5,
    "max_concurrent_trades": 2,
  },
  "entry_rules": [
    {"field": "rsi_14", "op": "<", "value_ref": "params.rsi_oversold",
     "side": "BUY"},
    ...
  ],
  "exit_rules": [
    {"kind": "take_profit_pct", "value": 1.0},
    {"kind": "stop_loss_pct",   "value": 0.5},
  ],
  "pseudocode": "..."        # Python-flavoured human-readable code block
}
"""
import os
import json
import re
import uuid
import logging

from emergentintegrations.llm.chat import LlmChat, UserMessage

logger = logging.getLogger("strategy-code-generator")


SYSTEM_PROMPT = """You are a quantitative trading strategy code generator.

Given a high-level compiled strategy (risk level, symbols, session, style),
expand it into a precise executable DSL the bot can interpret AND a
Python-flavoured pseudocode block humans can read.

You MUST output STRICT JSON (no prose, no markdown fences) with this shape:

{
  "version": "1.0",
  "name": "...short name (≤60 chars)...",
  "symbols": ["XAUUSD", "BTCUSD", ...],
  "session_preference": "london"|"ny"|"tokyo"|"any",
  "params": {
    "min_confidence": int 50..95,
    "rsi_oversold": int 20..40,
    "rsi_overbought": int 60..80,
    "atr_pct_max": float 0.5..3.0,
    "max_concurrent_trades": int 1..5
  },
  "entry_rules": [
    {"field": "<one of: rsi_14, atr_pct, ma_20_vs_200, session, confidence>",
     "op": "<one of: <, >, =, in, between>",
     "value_ref"?: "params.<key>",        // reference a param
     "value"?: <literal>,                   // OR a literal
     "side": "BUY"|"SELL"|"ANY",
     "description": "...short human reason..."}
  ],
  "exit_rules": [
    {"kind": "take_profit_pct"|"stop_loss_pct"|"breakeven_after_pct"|"trail_pct",
     "value": float}
  ],
  "pseudocode": "...4-12 line Python-flavoured block showing the logic..."
}

Strict rules:
- entry_rules length: 2-5 rules
- exit_rules length: 2-4 rules (must include both TP and SL)
- pseudocode MUST use real Python syntax with comments — readable by a human.
- Map the input strategy_style:
    trend_following → MA20 > MA200 entry, trailing exit
    mean_reversion  → RSI oversold/overbought entries, fixed TP
    scalping        → tight params, london/ny session, small TP/SL
    swing           → wider params, multi-session, larger TP
- Map risk_level → defaults:
    low      → min_confidence=85, atr_pct_max=1.0, TP=0.8%, SL=0.4%
    medium   → min_confidence=75, atr_pct_max=1.5, TP=1.2%, SL=0.6%
    high     → min_confidence=65, atr_pct_max=2.0, TP=2.0%, SL=1.0%
    extreme  → min_confidence=55, atr_pct_max=3.0, TP=3.0%, SL=1.5%
"""


ALLOWED_FIELDS = {"rsi_14", "atr_pct", "ma_20_vs_200", "session", "confidence"}
ALLOWED_OPS = {"<", ">", "=", "in", "between"}
ALLOWED_EXIT_KINDS = {"take_profit_pct", "stop_loss_pct",
                      "breakeven_after_pct", "trail_pct"}


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    return json.loads(text)


def _validate(dsl: dict) -> tuple[bool, str]:
    """Closed-vocab validation — anything outside the allow-list is rejected."""
    if not isinstance(dsl, dict):
        return False, "Output is not a JSON object"
    for k in ("version", "name", "symbols", "params",
              "entry_rules", "exit_rules", "pseudocode"):
        if k not in dsl:
            return False, f"missing key: {k}"
    if not isinstance(dsl["entry_rules"], list) or not dsl["entry_rules"]:
        return False, "entry_rules must be a non-empty list"
    if not isinstance(dsl["exit_rules"], list) or len(dsl["exit_rules"]) < 2:
        return False, "exit_rules must have at least TP and SL"
    for r in dsl["entry_rules"]:
        if r.get("field") not in ALLOWED_FIELDS:
            return False, f"entry_rule field '{r.get('field')}' not allowed"
        if r.get("op") not in ALLOWED_OPS:
            return False, f"entry_rule op '{r.get('op')}' not allowed"
        if r.get("side") not in ("BUY", "SELL", "ANY"):
            return False, f"entry_rule side '{r.get('side')}' invalid"
    for r in dsl["exit_rules"]:
        if r.get("kind") not in ALLOWED_EXIT_KINDS:
            return False, f"exit_rule kind '{r.get('kind')}' not allowed"
    params = dsl["params"]
    if not isinstance(params, dict):
        return False, "params must be a dict"
    # Range sanity — silently clamp rather than refuse so a near-miss
    # from the LLM still produces a usable DSL.
    params["min_confidence"]       = max(50, min(95, int(params.get("min_confidence", 75))))
    params["rsi_oversold"]         = max(20, min(40, int(params.get("rsi_oversold", 30))))
    params["rsi_overbought"]       = max(60, min(80, int(params.get("rsi_overbought", 70))))
    params["atr_pct_max"]          = max(0.5, min(3.0, float(params.get("atr_pct_max", 1.5))))
    params["max_concurrent_trades"] = max(1, min(5, int(params.get("max_concurrent_trades", 2))))
    return True, "ok"


async def generate_code(compiled: dict) -> dict:
    """Expand a compiled NL strategy into the DSL + pseudocode."""
    chat = LlmChat(
        api_key=os.environ["EMERGENT_LLM_KEY"],
        session_id=f"strat-code-{uuid.uuid4().hex[:8]}",
        system_message=SYSTEM_PROMPT,
    ).with_model("anthropic", "claude-sonnet-4-5-20250929")

    user_msg = ("Expand this compiled strategy into the full DSL + "
                "pseudocode per the system schema:\n\n"
                + json.dumps(compiled, indent=2))
    response = await chat.send_message(UserMessage(text=user_msg))
    try:
        dsl = _parse_json(str(response))
    except Exception as e:
        logger.warning("DSL parse failed: %s", e)
        return {"error": f"Code generator returned non-JSON: {e}",
                "raw": str(response)[:400]}

    ok, reason = _validate(dsl)
    if not ok:
        logger.warning("DSL validation failed: %s", reason)
        return {"error": f"Generated DSL failed validation: {reason}",
                "raw": dsl}
    return dsl
