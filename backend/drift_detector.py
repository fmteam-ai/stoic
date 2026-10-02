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

# iter-75b · river is heavyweight (~80MB) and is the only consumer of
# ADWIN in this codebase. Make the import optional so the bot runs in
# resource-constrained deployment environments without it. When absent,
# drift detection is a no-op and the bot still self-trains on the
# cadence dictated by MIN_SAMPLES thresholds in learned_meta.
try:
    from river.drift import ADWIN
    _DRIFT_LIB_AVAILABLE = True
except ImportError:
    ADWIN = None  # type: ignore[assignment]
    _DRIFT_LIB_AVAILABLE = False

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
# Global toggle. Default ON, but force OFF if `river` isn't available.
DRIFT_ENABLED = (
    os.environ.get("DRIFT_DETECTION_ENABLED", "true").lower() == "true"
    and _DRIFT_LIB_AVAILABLE
)


async def record_residual(
    db,
    *,
    trade_id: str,
    session: str,
    p_predicted: float,
    outcome: int,
    user_id: str | None = None,
    account_id: str | None = None,
) -> None:
    """Persist a single residual point. `outcome` ∈ {0,1}. `user_id` /
    `account_id` scope the residual so drift can retrain the owner's
    scoped learned_meta model (learned_meta.retrain(user, account))."""
    if not DRIFT_ENABLED:
        return
    try:
        await db.learned_meta_residuals.insert_one({
            "trade_id": str(trade_id),
            **({"user_id": str(user_id)} if user_id else {}),
            **({"account_id": str(account_id)} if account_id else {}),
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
                              p_predicted=float(p_pred), outcome=outcome,
                              user_id=trade.get("user_id"),
                              account_id=trade.get("account_id"))
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("record_residual_for_trade failed (%s)", e)
        return False


def _scope_filter(user_id: str | None = None,
                  account_id: str | None = None) -> dict:
    q: dict = {}
    if user_id:
        q["user_id"] = str(user_id)
        if account_id:
            q["account_id"] = str(account_id)
    return q


async def _fetch_residuals(db, session: str, limit: int,
                           scope: dict | None = None) -> list[float]:
    cur = db.learned_meta_residuals.find(
        {"session": session.upper(), **(scope or {})},
    ).sort("closed_at", -1).limit(limit)
    rows = await cur.to_list(length=limit)
    # We want chronological order for ADWIN (oldest → newest).
    rows.reverse()
    return [float(r.get("residual") or 0.0) for r in rows]


async def _check_session(db, session: str, scope: dict | None = None) -> dict:
    """Run ADWIN over the rolling window of one session's residuals.

    Returns {session, n, drift_detected, mean_residual, last_drift_at?}.
    """
    series = await _fetch_residuals(db, session, WINDOW_SIZE, scope)
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


async def check_drift(db, sessions: Iterable[str] = ("ASIA", "LONDON", "NY", "GLOBAL"),
                      *, user_id: str | None = None,
                      account_id: str | None = None) -> dict:
    """Read-only: compute drift status across sessions for inspection
    (optionally over one user's / account's residuals only)."""
    if not DRIFT_ENABLED:
        return {"enabled": False, "sessions": {}}
    scope = _scope_filter(user_id, account_id)
    results: dict = {}
    for sess in sessions:
        results[sess] = await _check_session(db, sess, scope)
    return {"enabled": True, "sessions": results,
            "window_size": WINDOW_SIZE, "min_samples": MIN_RESIDUALS_PER_SESSION,
            "adwin_delta": ADWIN_DELTA}


def _cooldown_key(user_id: str | None = None,
                  account_id: str | None = None) -> str:
    if not user_id:
        return "last_retrain"
    return f"last_retrain:{user_id}:{account_id or '*'}"


async def _cooldown_ok(db, key: str = "last_retrain") -> tuple[bool, datetime | None]:
    """Check whether the cooldown window has elapsed since the last retrain."""
    last = await db.drift_detector_state.find_one({"key": key})
    if not last:
        return True, None
    ts = last.get("at")
    if not isinstance(ts, datetime):
        return True, None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - ts
    return elapsed >= timedelta(hours=RETRAIN_COOLDOWN_HOURS), ts


# Upper bound on per-user scoped retrains fired by one GLOBAL drift event.
MAX_SCOPED_RETRAINS = int(os.environ.get("DRIFT_MAX_SCOPED_RETRAINS", "25"))


async def _scoped_retrains_for_drift(db, retrain) -> list:
    """A global drift event also retrains every recently-active owner's
    SCOPED model (learned_meta.retrain(user, account)) — the scoped
    artifacts are what predict_p_win prefers. Advisory: never raises."""
    out: list = []
    try:
        rows = await db.learned_meta_residuals.find(
            {"user_id": {"$exists": True}},
            {"user_id": 1, "account_id": 1},
        ).sort("closed_at", -1).limit(WINDOW_SIZE).to_list(length=WINDOW_SIZE)
    except Exception as e:  # noqa: BLE001
        logger.debug("scoped drift retrain scan failed (%s)", e)
        return out
    seen: list = []
    for r in rows or []:
        pair = (r.get("user_id"), r.get("account_id"))
        if pair[0] and pair not in seen:
            seen.append(pair)
    for uid, acct in seen[:MAX_SCOPED_RETRAINS]:
        try:
            res = await retrain(uid, acct)
            out.append({"user_id": uid, "account_id": acct,
                        "trained": (res or {}).get("trained")})
        except Exception as e:  # noqa: BLE001
            logger.warning("scoped drift retrain failed user=%s acct=%s: %s",
                           uid, acct, e)
    return out


async def maybe_trigger_retrain(db, user_id: str | None = None,
                                account_id: str | None = None) -> dict:
    """Run drift checks; if any session drifts AND cooldown elapsed, retrain.

    With `user_id` (and optionally `account_id`) the drift check runs over
    that owner's residuals only and the owner's SCOPED learned_meta model is
    retrained; without, the global stream is checked and the legacy global
    model plus every recently-active owner's scoped model are retrained.

    Returns audit dict suitable for logging / API response.
    """
    if not DRIFT_ENABLED:
        return {"checked": False, "reason": "drift detection disabled"}

    status = await check_drift(db, user_id=user_id, account_id=account_id)
    any_drift = any(s.get("drift_detected") for s in status["sessions"].values())
    if not any_drift:
        return {"checked": True, "drift": False, "retrained": False,
                "status": status}

    ckey = _cooldown_key(user_id, account_id)
    ok, last_at = await _cooldown_ok(db, ckey)
    if not ok:
        return {"checked": True, "drift": True, "retrained": False,
                "reason": f"cooldown active — last retrain at {last_at.isoformat()}",
                "status": status}

    # Trigger retrain — local import to avoid circular dependency.
    from learned_meta import retrain
    scoped: list = []
    try:
        if user_id:
            result = await retrain(user_id, account_id)
        else:
            result = await retrain()
            scoped = await _scoped_retrains_for_drift(db, retrain)
    except Exception as e:  # noqa: BLE001
        logger.exception("Drift-triggered retrain failed")
        return {"checked": True, "drift": True, "retrained": False,
                "reason": f"retrain raised: {e}", "status": status}

    # Persist cooldown anchor + audit.
    now = datetime.now(timezone.utc)
    await db.drift_detector_state.update_one(
        {"key": ckey},
        {"$set": {"at": now, "result": result, "trigger": "adwin_drift",
                  "user_id": user_id, "account_id": account_id}},
        upsert=True,
    )
    await db.drift_detector_audit.insert_one({
        "at": now, "status": status, "retrain_result": result,
        "user_id": user_id, "account_id": account_id,
        "scoped_retrains": scoped,
    })
    logger.warning("ADWIN drift → retrained learned_meta (trained=%s)", result.get("trained"))
    return {"checked": True, "drift": True, "retrained": True,
            "retrain_result": result, "status": status,
            **({"scoped_retrains": scoped} if scoped else {})}
