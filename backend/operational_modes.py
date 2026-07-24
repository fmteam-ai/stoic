"""Autopilot #15 — explicit operational modes.

    observe          detect opportunities, never create trades
    shadow           record full simulated decisions from live data
    demo_autopilot   full system trades on demo accounts only
    supervised_live  real capital at half size (operator available)
    autonomous_live  approved strategies trade automatically (default)
    defensive        only manage/reduce existing exposure — no new entries
    panic            block new trades (position closing via the panic switch)

The gate runs AFTER the full analysis pipeline so observe/shadow modes still
produce studyable decisions — they are recorded in `mode_intercepts`.
"""
from datetime import datetime, timezone

MODES = {
    "observe": {"rank": 2, "allow_new": False, "lot_scale": 0.0,
                "label": "OBSERVE",
                "detail": "opportunities detected and recorded — no orders created"},
    "shadow": {"rank": 3, "allow_new": False, "lot_scale": 0.0,
               "label": "SHADOW",
               "detail": "full simulated decisions recorded from live data"},
    "demo_autopilot": {"rank": 4, "allow_new": True, "lot_scale": 1.0,
                       "label": "DEMO AUTOPILOT", "demo_only": True,
                       "detail": "full system trades on demo accounts only"},
    "supervised_live": {"rank": 5, "allow_new": True, "lot_scale": 0.5,
                        "label": "SUPERVISED LIVE",
                        "detail": "real capital at half size while an operator is available"},
    "autonomous_live": {"rank": 6, "allow_new": True, "lot_scale": 1.0,
                        "label": "AUTONOMOUS LIVE",
                        "detail": "approved strategies trade automatically"},
    "defensive": {"rank": 1, "allow_new": False, "lot_scale": 0.0,
                  "label": "DEFENSIVE",
                  "detail": "only manages and reduces existing exposure"},
    "panic": {"rank": 0, "allow_new": False, "lot_scale": 0.0,
              "label": "PANIC",
              "detail": "new trades blocked — use the PANIC switch to flatten"},
}
DEFAULT_MODE = "autonomous_live"


def mode_gate(cfg: dict, account: dict) -> dict:
    """→ {mode, allow_new, lot_scale, reason}. Unknown mode fails closed
    to observe (never trade on a misconfigured mode)."""
    mode = (cfg or {}).get("operational_mode") or DEFAULT_MODE
    spec = MODES.get(mode)
    if spec is None:
        return {"mode": "observe", "allow_new": False, "lot_scale": 0.0,
                "reason": f"unknown mode '{mode}' — failing closed to observe"}
    if spec.get("demo_only") and str(
            (account or {}).get("mode") or "").lower() == "live":
        return {"mode": mode, "allow_new": False, "lot_scale": 0.0,
                "reason": "demo autopilot — live accounts do not execute"}
    return {"mode": mode, "allow_new": spec["allow_new"],
            "lot_scale": spec["lot_scale"], "reason": spec["detail"]}


async def record_intercept(db, user_id: str, account: dict, signal: dict,
                           lot: float, mg: dict) -> None:
    """Observe/shadow/defensive decisions are valuable — keep them."""
    await db.mode_intercepts.insert_one({
        "user_id": user_id,
        "account_id": str((account or {}).get("_id") or ""),
        "mode": mg["mode"],
        "symbol": signal.get("symbol"),
        "action": signal.get("action"),
        "confidence": signal.get("confidence"),
        "entry_price": signal.get("entry_price"),
        "stop_loss": signal.get("stop_loss"),
        "take_profit": signal.get("take_profit"),
        "lot_size": lot,
        "scope": signal.get("scope"),
        "consensus": (signal.get("consensus") or {}).get("score"),
        "monte_carlo_ev_r": (signal.get("monte_carlo") or {}).get(
            "ev_r_net", (signal.get("monte_carlo") or {}).get("ev_r")),
        "at": datetime.now(timezone.utc).isoformat(),
    })
