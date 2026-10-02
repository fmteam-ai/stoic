"""Hypothesis Generator — Claude proposes strategy tweaks given weaknesses.

Takes the trade analyzer's weakness report + the user's currently active
bot config and asks Claude to suggest 3-5 *small, testable* hypotheses.
Each hypothesis is a fully-formed compiled strategy (same shape as
`nl_commander.build_strategy`) the auto-backtester can immediately run.

Strict JSON output:
  {
    "hypotheses": [
      {
        "name":        str,
        "rationale":   str,    # ≤200 chars — why this might help
        "compiled": {          # same shape as nl_commander.build_strategy output
          "symbols": [str],
          "session_preference": "london"|"ny"|"tokyo"|"any",
          "risk_level": "low"|"medium"|"high"|"extreme",
          "strategy_style": "trend_following"|"mean_reversion"|"scalping"|"swing",
          "max_concurrent_trades": int 1..5,
          "auto_execute": bool,
        },
      },
      ...
    ]
  }
"""
import os
import json
import re
import uuid
import logging

from emergentintegrations.llm.chat import LlmChat, UserMessage
from llm_models import provider_model

logger = logging.getLogger("research.hypothesis-generator")


SYSTEM_PROMPT = """You are a quantitative trading researcher.

Given:
  • a 30-day weakness report from a live trading bot's closed trades
  • the bot's CURRENT compiled strategy

Generate 3-5 small, *testable* improvement hypotheses. Each hypothesis must
be a fully-specified compiled strategy that differs from the current one
in ONE focused dimension (drop a losing symbol, switch session, drop risk
level, change style, reduce concurrent trades).

Avoid:
  - "Use a completely different strategy" — make focused, surgical changes
  - Symbols outside the user's existing universe — stay within their current symbol set
  - Vague rationale — be specific about which weakness it addresses

Output STRICT JSON (no prose, no markdown fences):

{
  "hypotheses": [
    {
      "name": "...short name (≤60 chars)...",
      "rationale": "...≤200 chars explaining the trade-off addressed...",
      "compiled": {
        "symbols": [...subset of user's current symbols...],
        "session_preference": "london"|"ny"|"tokyo"|"any",
        "risk_level": "low"|"medium"|"high"|"extreme",
        "strategy_style": "trend_following"|"mean_reversion"|"scalping"|"swing",
        "max_concurrent_trades": 1..5,
        "auto_execute": false
      }
    }
  ]
}

Allowed session_preference: london | ny | tokyo | any
Allowed risk_level:        low | medium | high | extreme
Allowed strategy_style:    trend_following | mean_reversion | scalping | swing
"""

_ALLOWED_SESSION = {"london", "ny", "tokyo", "any"}
_ALLOWED_RISK    = {"low", "medium", "high", "extreme"}
_ALLOWED_STYLE   = {"trend_following", "mean_reversion", "scalping", "swing"}


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    return json.loads(text)


def _validate(payload: dict, user_symbols: list[str]) -> tuple[list[dict], list[str]]:
    """Return (valid_hypotheses, warnings). Quietly drops invalid ones."""
    warnings: list[str] = []
    out: list[dict] = []
    for h in (payload.get("hypotheses") or [])[:8]:
        c = h.get("compiled") or {}
        sess  = (c.get("session_preference") or "any").lower()
        risk  = (c.get("risk_level") or "medium").lower()
        style = (c.get("strategy_style") or "trend_following").lower()
        syms  = [s.upper() for s in (c.get("symbols") or []) if s]
        # Filter to symbols the user actually trades — no scope creep
        syms = [s for s in syms if s in user_symbols]
        if not syms:
            warnings.append(f"Skipped hypothesis '{h.get('name')}': no valid symbols")
            continue
        if sess not in _ALLOWED_SESSION:
            sess = "any"
        if risk not in _ALLOWED_RISK:
            risk = "medium"
        if style not in _ALLOWED_STYLE:
            style = "trend_following"
        try:
            mct = max(1, min(5, int(c.get("max_concurrent_trades", 2))))
        except Exception:
            mct = 2
        out.append({
            "name": (h.get("name") or "Unnamed")[:60],
            "rationale": (h.get("rationale") or "")[:200],
            "compiled": {
                "symbols": syms,
                "session_preference": sess,
                "risk_level": risk,
                "strategy_style": style,
                "max_concurrent_trades": mct,
                "auto_execute": False,  # safety — proposals never auto-execute
            },
        })
    return out, warnings


async def generate(
    *,
    weaknesses: dict,
    current_strategy: dict,
    user_symbols: list[str],
    max_hypotheses: int = 5,
) -> dict:
    """Call Claude with the weaknesses + current strategy → return validated hypotheses."""
    if not user_symbols:
        return {"hypotheses": [],
                "notes": ["No symbols configured — nothing to propose."]}

    chat = LlmChat(
        api_key=os.environ["EMERGENT_LLM_KEY"],
        session_id=f"self-improve-{uuid.uuid4().hex[:8]}",
        system_message=SYSTEM_PROMPT,
    ).with_model(*provider_model("hypothesis_generator"))

    user_msg = (
        "WEAKNESS REPORT (last 30d):\n"
        f"{json.dumps(weaknesses, indent=2, default=str)}\n\n"
        f"CURRENT COMPILED STRATEGY:\n{json.dumps(current_strategy, indent=2)}\n\n"
        f"USER'S TRADED SYMBOLS: {user_symbols}\n\n"
        f"Generate up to {max_hypotheses} focused improvement hypotheses per the system schema."
    )
    response = await chat.send_message(UserMessage(text=user_msg))
    try:
        parsed = _parse_json(str(response))
    except Exception as e:  # noqa: BLE001
        logger.warning("Hypothesis JSON parse failed: %s", e)
        return {"hypotheses": [],
                "notes": [f"LLM returned non-JSON: {e}"]}

    valid, warns = _validate(parsed, user_symbols)
    return {"hypotheses": valid[:max_hypotheses], "notes": warns}
