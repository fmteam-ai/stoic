"""Online drift detection for the learned-meta classifier (iter-52).

Uses ADWIN (Adaptive Windowing) from the `river` streaming-ML library
to monitor the **prediction-residual stream** per session bucket.

Residual definition: `r_t = |p_win_predicted − actual_outcome|`
where `actual_outcome` ∈ {0, 1} after a trade closes (1 = win, 0 = loss).
A drifting model produces residuals whose mean shifts — ADWIN detects
the change-point with bounded false-positive rate δ.

Pipeline
========
1. Bot closes a trade → `record_residual(...)` stamps an entry in
   `db.learned_meta_residuals` with {session, p_predicted, outcome,
   residual, trade_id, closed_at}.
2. Every loop tick, `maybe_trigger_retrain(...)` rebuilds the ADWIN
   detectors from the last N residuals per session. If any detector
   reports `drift_detected` AND the cooldown has elapsed, the global
   retrain is fired and the detection is audit-logged.
3. State (last retrain time per session) is persisted to
   `db.drift_detector_state` so we don't thrash retrains.

We rebuild ADWIN from history on every check rather than persisting
detector state — ADWIN's window is bounded so this is cheap, and it
avoids brittle serialisation of `river`'s internal buckets.
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Iterable

from river.drift import ADWIN

from database import get_db

logger = logging.getLogger("drift-detector")

# Minimum residuals required before we'll even run a session's detector.
MIN_RESIDUALS_PER_SESSION = int(os.environ.get("DRIFT_MIN_RESIDUALS", "30"))
# Window of most-recent residuals to feed ADWIN per check (rolling).
WINDOW_SIZE = int(os.environ.get("DRIFT_WINDOW_SIZE", "300"))
# ADWIN false-positive control. Smaller = more conservative.
ADWIN_DELTA = float(os.environ.get("DRIFT_ADWIN_DELTA", "0.002"))
# Cooldown — once we retrain, don't retrain again for this many hours.
RETRAIN_COOLDOWN_HOURS = float(os.environ.get("DRIFT_RETRAIN_COOLDOWN_HRS", "12"))
# Global toggle. Default ON.
DRIFT_ENABLED = os.environ.get("DRIFT_DETECTION_ENABLED", "true").lower() == "true"


async def record_residual(
    db,
    *,
    trade_id: str,
    session: str,
    p_predicted: float,
    outcome: int,
) -> None:
    """Persist a single residual point. `outcome` ∈ {0,1}."""
    if not DRIFT_ENABLED:
        return
    try:
        await db.learned_meta_residuals.insert_one({
            "trade_id": str(trade_id),
            "session": (session or "GLOBAL").upper(),
            "p_predicted": float(p_predicted),
            "outcome": int(1 if outcome else 0),
            "residual": abs(float(p_predicted) - float(1 if outcome else 0)),
            "closed_at": datetime.now(timezone.utc),
        })
    except Exception as e:  # noqa: BLE001
        logger.debug("record_residual failed (%s) — skipping", e)


async def record_residual_for_trade(db, trade_id) -> bool:
    """Fire-and-forget helper called from trade-close paths.

    Looks up the trade + its originating signal, extracts the learned-meta
    `p_win` snapshot, derives the session bucket from the entry time, and
    inserts a residual row. Skips silently if any required field is absent
    or a residual already exists for this trade.
    """
    if not DRIFT_ENABLED:
        return False
    try:
        from bson import ObjectId as _OID
        try:
            oid = _OID(str(trade_id))
        except Exception:
            return False
        trade = await db.trades.find_one({"_id": oid})
        if not trade or trade.get("status") != "closed":
            return False
        existing = await db.learned_meta_residuals.find_one({"trade_id": str(oid)})
        if existing:
            return False
        sig_id = trade.get("signal_id")
        if not sig_id:
            return False
        try:
            sig = await db.signals.find_one({"_id": _OID(str(sig_id))})
        except Exception:
            sig = None
        if not sig:
            return False
        lm = sig.get("learned_meta") or {}
        # Prefer the calibrated probability when available, fall back to raw.
        p_pred = lm.get("p_win_calibrated") or lm.get("p_win") or lm.get("p_win_raw")
        if p_pred is None:
            return False
        # Session bucket from entry time — keep consistent with learned_meta._build_dataset.
        from learned_meta import session_bucket
        entered_at = trade.get("entered_at") or trade.get("opened_at") or sig.get("created_at")
        session = session_bucket(entered_at)
        outcome = 1 if float(trade.get("pnl") or 0) > 0 else 0
        await record_residual(db, trade_id=str(oid), session=session,
                              p_predicted=float(p_pred), outcome=outcome)
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("record_residual_for_trade failed (%s)", e)
        return False


async def _fetch_residuals(db, session: str, limit: int) -> list[float]:
    cur = db.learned_meta_residuals.find(
        {"session": session.upper()},
    ).sort("closed_at", -1).limit(limit)
    rows = await cur.to_list(length=limit)
    # We want chronological order for ADWIN (oldest → newest).
    rows.reverse()
    return [float(r.get("residual") or 0.0) for r in rows]


async def _check_session(db, session: str) -> dict:
    """Run ADWIN over the rolling window of one session's residuals.

    Returns {session, n, drift_detected, mean_residual, last_drift_at?}.
    """
    series = await _fetch_residuals(db, session, WINDOW_SIZE)
    n = len(series)
    out = {"session": session, "n": n, "drift_detected": False,
           "mean_residual": 0.0}
    if n < MIN_RESIDUALS_PER_SESSION:
        out["reason"] = f"need ≥{MIN_RESIDUALS_PER_SESSION} samples (have {n})"
        return out

    adw = ADWIN(delta=ADWIN_DELTA)
    drifts_at: list[int] = []
    for i, r in enumerate(series):
        adw.update(r)
        if adw.drift_detected:
            drifts_at.append(i)
    out["drift_detected"] = bool(drifts_at)
    out["drifts_at_indices"] = drifts_at[-5:]
    out["mean_residual"] = round(sum(series) / n, 4)
    return out


async def check_drift(db, sessions: Iterable[str] = ("ASIA", "LONDON", "NY", "GLOBAL")) -> dict:
    """Read-only: compute drift status across sessions for inspection."""
    if not DRIFT_ENABLED:
        return {"enabled": False, "sessions": {}}
    results: dict = {}
    for sess in sessions:
        results[sess] = await _check_session(db, sess)
    return {"enabled": True, "sessions": results,
            "window_size": WINDOW_SIZE, "min_samples": MIN_RESIDUALS_PER_SESSION,
            "adwin_delta": ADWIN_DELTA}


async def _cooldown_ok(db) -> tuple[bool, datetime | None]:
    """Check whether the cooldown window has elapsed since the last retrain."""
    last = await db.drift_detector_state.find_one({"key": "last_retrain"})
    if not last:
        return True, None
    ts = last.get("at")
    if not isinstance(ts, datetime):
        return True, None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - ts
    return elapsed >= timedelta(hours=RETRAIN_COOLDOWN_HOURS), ts


async def maybe_trigger_retrain(db) -> dict:
    """Run drift checks; if any session drifts AND cooldown elapsed, retrain.

    Returns audit dict suitable for logging / API response.
    """
    if not DRIFT_ENABLED:
        return {"checked": False, "reason": "drift detection disabled"}

    status = await check_drift(db)
    any_drift = any(s.get("drift_detected") for s in status["sessions"].values())
    if not any_drift:
        return {"checked": True, "drift": False, "retrained": False,
                "status": status}

    ok, last_at = await _cooldown_ok(db)
    if not ok:
        return {"checked": True, "drift": True, "retrained": False,
                "reason": f"cooldown active — last retrain at {last_at.isoformat()}",
                "status": status}

    # Trigger retrain — local import to avoid circular dependency.
    from learned_meta import retrain
    try:
        result = await retrain()
    except Exception as e:  # noqa: BLE001
        logger.exception("Drift-triggered retrain failed")
        return {"checked": True, "drift": True, "retrained": False,
                "reason": f"retrain raised: {e}", "status": status}

    # Persist cooldown anchor + audit.
    now = datetime.now(timezone.utc)
    await db.drift_detector_state.update_one(
        {"key": "last_retrain"},
        {"$set": {"at": now, "result": result, "trigger": "adwin_drift"}},
        upsert=True,
    )
    await db.drift_detector_audit.insert_one({
        "at": now, "status": status, "retrain_result": result,
    })
    logger.warning("ADWIN drift → retrained learned_meta (trained=%s)", result.get("trained"))
    return {"checked": True, "drift": True, "retrained": True,
            "retrain_result": result, "status": status}
