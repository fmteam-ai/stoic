"""ExecutionOptimizerAgent — last-mile timing/sizing optimizer.

Runs AFTER risk + portfolio-allocator approve a trade but BEFORE the
broker engine fires. Three deterministic guards:

  1. SPREAD GUARD     — refuse to fire when the live spread on this
                         symbol is above a multiple of the account's
                         rolling-median spread. (Brokers occasionally
                         flare the spread to 5-10× the norm during
                         news; firing there costs you the edge.)
  2. SESSION GUARD    — for XAUUSD, defer trades fired in pure
                         off-hours (e.g., 22:00-06:00 UTC, weekends)
                         unless aggressive_mode is set. Off-hours
                         spreads are typically 2-3× tighter sessions.
  3. SLICE PLANNER    — if the lot would represent > SLICE_MAX_PCT of
                         the symbol's average daily volume budget (a
                         proxy: lot * 100 vs 5× equity), produce a
                         slice plan (e.g., 0.10 → 2 × 0.05) the engine
                         can iterate over.

The agent NEVER overrides the strategy's direction or stops — it only
shapes timing & slicing. If a guard fires, the orchestrator can either:
  - DEFER the trade (skip this tick — re-evaluated next tick) — current default
  - PROCEED with the slice plan even when guards fire

Output:
  {
    "approved": bool,            # False → caller should defer
    "deferred_reason": str|None,
    "slice_plan": [float, ...] | None,
    "spread": {observed, median, ratio}|None,
    "session": str,
    "bias": str,                 # one-liner for the activity log
  }
"""
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger("agent.execution-optimizer")


def _f(env_key: str, default: float) -> float:
    try:
        return float(os.environ.get(env_key, default))
    except Exception:
        return default


def _bool(env_key: str, default: bool) -> bool:
    v = os.environ.get(env_key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


# Tunables — env-overridable so power-users can loosen them.
SPREAD_MAX_MULTIPLE = _f("EXEC_SPREAD_MAX_MULTIPLE", 2.5)   # refuse > 2.5× median
SLICE_LOT_THRESHOLD = _f("EXEC_SLICE_LOT_THRESHOLD", 0.10)  # split lots > 0.10
SLICE_CHUNK         = _f("EXEC_SLICE_CHUNK", 0.05)          # 0.05 chunks
DEFER_OFF_HOURS_GOLD = _bool("EXEC_DEFER_OFF_HOURS_GOLD", True)


# Approximate UTC ranges for XAUUSD off-hours (very low liquidity, wide spreads).
def _is_xau_off_hours(now: datetime | None = None) -> bool:
    n = now or datetime.now(timezone.utc)
    if n.weekday() >= 5:  # Sat/Sun
        return True
    h = n.hour
    # Off-hours: 22:00-06:00 UTC (post-NY close to pre-London open)
    return h >= 22 or h < 6


def _slice_plan(lot: float) -> list[float] | None:
    if lot <= SLICE_LOT_THRESHOLD:
        return None
    chunks: list[float] = []
    remaining = lot
    while remaining > SLICE_CHUNK + 1e-9:
        chunks.append(round(SLICE_CHUNK, 4))
        remaining -= SLICE_CHUNK
    if remaining > 1e-9:
        chunks.append(round(remaining, 4))
    return chunks if len(chunks) > 1 else None


class ExecutionOptimizerAgent:
    name = "execution_optimizer"

    def __init__(self):
        self.spread_max_multiple = SPREAD_MAX_MULTIPLE

    def _spread_guard(self, *, symbol: str, account: dict | None) -> dict | None:
        """Returns a guard-fail dict if the live spread is too wide, else None."""
        if not account:
            return None
        spreads = account.get("current_spreads") or {}
        median_spreads = account.get("median_spreads") or {}
        observed = spreads.get(symbol)
        median = median_spreads.get(symbol)
        if observed is None or median is None or median <= 0:
            return None
        ratio = float(observed) / float(median)
        if ratio > self.spread_max_multiple:
            return {
                "spread": {"observed": float(observed), "median": float(median),
                           "ratio": round(ratio, 2)},
                "reason": (f"Spread {observed:.2f} > {self.spread_max_multiple:.1f}× "
                           f"rolling median {median:.2f} (×{ratio:.1f})"),
            }
        return {"spread": {"observed": float(observed), "median": float(median),
                           "ratio": round(ratio, 2)},
                "reason": None}

    async def optimize(
        self,
        *,
        signal: dict,
        account: dict | None = None,
        aggressive_mode: bool = False,
    ) -> dict:
        action = (signal or {}).get("action") or "HOLD"
        symbol = (signal or {}).get("symbol") or ""
        sym = symbol.upper()
        lot = float((signal or {}).get("lot_size") or 0.0)

        if action not in ("BUY", "SELL") or lot <= 0:
            return {"approved": True, "deferred_reason": None,
                    "slice_plan": None, "spread": None,
                    "session": "n/a", "bias": "pass-through"}

        # 1. Spread guard
        spread_info = None
        spread_result = self._spread_guard(symbol=sym, account=account)
        deferred_reason = None
        if spread_result:
            spread_info = spread_result.get("spread")
            if spread_result.get("reason"):
                deferred_reason = spread_result["reason"]

        # 2. Session guard — XAUUSD off-hours
        session = "active"
        if sym == "XAUUSD" and DEFER_OFF_HOURS_GOLD and _is_xau_off_hours():
            session = "off_hours"
            if not aggressive_mode and deferred_reason is None:
                deferred_reason = ("XAUUSD off-hours window (22-06 UTC / weekend) "
                                   "— defer to next session for tighter spreads")

        # 3. Slice plan (informational — engine may or may not consume)
        plan = _slice_plan(lot)

        approved = deferred_reason is None
        bias_parts = []
        if not approved:
            bias_parts.append(f"DEFER: {deferred_reason[:60]}")
        if plan:
            bias_parts.append(f"slice {len(plan)}×{SLICE_CHUNK}")
        if spread_info and approved:
            bias_parts.append(f"spread ×{spread_info['ratio']}")
        if not bias_parts:
            bias_parts.append("clear")

        return {
            "approved": approved,
            "deferred_reason": deferred_reason,
            "slice_plan": plan,
            "spread": spread_info,
            "session": session,
            "bias": " · ".join(bias_parts),
        }
