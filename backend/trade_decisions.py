"""iter-134 · Canonical trade-decision ledger (quant roadmap #2, #3, #6).

One immutable document per BUY/SELL candidate outcome: which stage approved
or rejected it, why, under which strategy/risk/execution versions, and (for
executed trades) the final order result. Never overwritten — unlike the
bot-pulse, which only keeps the latest verdict.
"""
import logging
from datetime import datetime, timezone

from versioning import version_stamp

logger = logging.getLogger(__name__)

SNAPSHOT_FIELDS = ("action", "entry_price", "stop_loss", "take_profit",
                   "tp1", "tp2", "tp3", "confidence", "rr_ratio", "scope",
                   "entry_style", "session_feats", "news_bias",
                   "news_size_scale", "trend_ride", "regime",
                   "pipeline_safe_to_execute")


def _snapshot(signal: dict | None) -> dict:
    if not signal:
        return {}
    snap = {k: signal.get(k) for k in SNAPSHOT_FIELDS if signal.get(k) is not None}
    mc = signal.get("monte_carlo")
    if mc:
        snap["mc"] = {k: mc.get(k) for k in ("ev_r", "ev_r_net", "cost_r",
                                             "p_tp_first", "p_sl_first", "rr")}
    na = signal.get("news_ai")
    if na:
        snap["news_net"] = na.get("net")
    return snap


async def record_decision(db, *, user_id: str, symbol: str, status: str,
                          stage: str, reason: str, cfg: dict | None = None,
                          signal: dict | None = None,
                          execution: dict | None = None) -> None:
    """status: rejected | executed | advisory · stage: gate name / 'execution'."""
    try:
        doc = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
            "account_id": str((cfg or {}).get("account_id") or "") or None,
            "symbol": symbol,
            "status": status,
            "stage": stage,
            "reason": (reason or "")[:600],
            "signal": _snapshot(signal),
            "versions": version_stamp((signal or {}).get("scope")),
        }
        if execution:
            doc["execution"] = execution
        await db.trade_decisions.insert_one(doc)
    except Exception as e:  # noqa: BLE001
        logger.warning("decision ledger write failed: %s", e)


def infer_stage(reason: str) -> str:
    """Map a veto reason string to its gate name for querying/attribution."""
    r = (reason or "").lower()
    for needle, stage in (
        ("session-trend gate", "session_trend_gate"),
        ("exhaustion gate", "exhaustion_chase_gate"),
        ("monte carlo gate", "monte_carlo_gate"),
        ("news gate", "news_gate"),
        ("narrative risk", "narrative_risk"),
        ("structure", "structure_gate"),
        ("correlation", "correlation_guard"),
        ("anti-pyramid", "anti_pyramid"),
        ("anti-tilt", "anti_tilt"),
        ("cooldown", "cooldown"),
        ("spread", "spread_guard"),
        ("slippage", "slippage_guard"),
        ("friday", "friday_flat"),
        ("eod", "eod_quiet"),
        ("event", "calendar_guard"),
        ("risk engine", "risk_engine"),
        ("drawdown", "drawdown_guard"),
        ("daily loss", "daily_loss_guard"),
        ("exposure", "exposure_cap"),
        ("payoff", "payoff_guard"),
        ("r:r", "rr_guard"),
        ("knife", "knife_filter"),
        ("safety guardian", "safety_guardian"),
        ("kill", "kill_switch"),
    ):
        if needle in r:
            return stage
    return "gate"
