"""TWAP / VWAP scheduler — build a slice schedule for a large order.

TWAP (Time-Weighted Average Price): equal lot per equal time interval.
VWAP (Volume-Weighted Average Price): lots weighted by intra-day volume
profile. We don't have real volume data per minute, so we approximate
the curve with hardcoded session weights — london/ny overlap gets the
biggest weight, asian sessions get less.

Schedule shape:
  [
    {"idx": 0, "lot": 0.025, "fire_at": iso, "weight": 0.25},
    {"idx": 1, "lot": 0.025, "fire_at": iso, "weight": 0.25},
    ...
  ]

A schedule is persisted to `db.slice_schedules` so the bot_runner cron
can wake up and fire the slice when `fire_at` ≤ now AND status="pending".
"""
from datetime import datetime, timezone, timedelta
from typing import Literal
import logging
import uuid

logger = logging.getLogger("execution.twap-vwap")


# Hardcoded intra-day volume curve (24 buckets, one per hour UTC).
# Roughly: Tokyo modest, London ramp, London-NY overlap peak, NY taper, off-hours flat.
VOLUME_PROFILE_24H = [
    0.6, 0.6, 0.7, 0.8, 0.9, 1.0,    # 00-05 UTC — Asia tail
    1.2, 1.5, 1.8, 2.0, 2.0, 2.0,    # 06-11 UTC — London ramp
    2.5, 2.8, 3.0, 2.8, 2.5, 2.0,    # 12-17 UTC — London/NY overlap PEAK
    1.6, 1.2, 0.9, 0.8, 0.7, 0.6,    # 18-23 UTC — NY taper / weekend-quiet
]


def _vwap_weights_for_window(start: datetime, minutes: int, slices: int) -> list[float]:
    """Sample the 24h volume profile across the [start, start+minutes] window
    and renormalize so the weights sum to 1.0."""
    sample_step = max(1, minutes // slices)
    raw: list[float] = []
    for i in range(slices):
        t = start + timedelta(minutes=i * sample_step)
        raw.append(VOLUME_PROFILE_24H[t.astimezone(timezone.utc).hour])
    s = sum(raw) or 1.0
    return [r / s for r in raw]


def build_schedule(
    *,
    total_lot: float,
    duration_minutes: int,
    strategy: Literal["TWAP", "VWAP"] = "TWAP",
    slices: int = 4,
    start_at: datetime | None = None,
) -> dict:
    """Build a schedule of {idx, lot, fire_at, weight} entries."""
    if total_lot <= 0 or duration_minutes <= 0 or slices <= 0:
        return {"schedule_id": "", "strategy": strategy, "slices": [],
                "total_lot": total_lot, "duration_minutes": duration_minutes,
                "notes": "Invalid inputs — nothing to schedule."}

    now = start_at or datetime.now(timezone.utc)
    step_min = duration_minutes / slices

    if strategy == "VWAP":
        weights = _vwap_weights_for_window(now, duration_minutes, slices)
    else:  # TWAP — equal weights
        weights = [1.0 / slices] * slices

    out: list[dict] = []
    accumulated = 0.0
    for i in range(slices):
        # Last slice absorbs any rounding remainder
        if i == slices - 1:
            lot = round(total_lot - accumulated, 4)
        else:
            lot = round(total_lot * weights[i], 4)
            accumulated += lot
        if lot <= 0:
            continue
        fire_at = (now + timedelta(minutes=step_min * i)).isoformat()
        out.append({
            "idx": i,
            "lot": lot,
            "fire_at": fire_at,
            "weight": round(weights[i], 4),
            "status": "pending",
        })

    return {
        "schedule_id": uuid.uuid4().hex,
        "strategy": strategy,
        "slices": out,
        "total_lot": total_lot,
        "duration_minutes": duration_minutes,
        "created_at": now.isoformat(),
    }


async def persist(db, *, schedule: dict, signal: dict, user_id: str, account_id: str) -> str:
    """Save the schedule to mongo and return its id."""
    if not schedule.get("slices"):
        return ""
    doc = {
        "schedule_id":      schedule["schedule_id"],
        "user_id":          user_id,
        "account_id":       account_id,
        "symbol":           signal.get("symbol"),
        "action":           signal.get("action"),
        "strategy":         schedule["strategy"],
        "slices":           schedule["slices"],
        "total_lot":        schedule["total_lot"],
        "duration_minutes": schedule["duration_minutes"],
        "status":           "active",
        "created_at":       schedule.get("created_at") or datetime.now(timezone.utc).isoformat(),
    }
    await db.slice_schedules.insert_one(doc)
    return schedule["schedule_id"]


async def due_slices(db, *, user_id: str) -> list[dict]:
    """Return slices whose fire_at ≤ now and status=pending, for this user."""
    now_iso = datetime.now(timezone.utc).isoformat()
    cursor = db.slice_schedules.find({
        "user_id": user_id, "status": "active",
        "slices.fire_at": {"$lte": now_iso},
        "slices.status": "pending",
    }).limit(20)
    out: list[dict] = []
    async for sched in cursor:
        for s in sched.get("slices") or []:
            if s.get("status") == "pending" and s.get("fire_at") <= now_iso:
                out.append({
                    "schedule_id": sched["schedule_id"],
                    "symbol":      sched["symbol"],
                    "action":      sched["action"],
                    "account_id":  sched["account_id"],
                    "strategy":    sched["strategy"],
                    "slice_idx":   s["idx"],
                    "lot":         s["lot"],
                    "fire_at":     s["fire_at"],
                })
    return out
