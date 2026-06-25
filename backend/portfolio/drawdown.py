"""Peak-to-trough drawdown tracker.

Maintains a per-account equity high-water mark (HWM) and computes the
current drawdown from peak. Persisted to `db.accounts.equity_hwm` so the
tracker survives restarts and only updates UP (peak ratchets, never
walks back without an explicit reset).

Two thresholds (both env-overridable):
  • SOFT_DD_PCT (default 8%)  — warn the user, no automatic action
  • HARD_DD_PCT (default 15%) — auto-deleveraging engine kicks in

Returns a snapshot dict consumed by the risk manager + dashboard.
"""
from datetime import datetime, timezone
import logging
import os

logger = logging.getLogger("portfolio.drawdown")


def _f(env_key: str, default: float) -> float:
    try:
        return float(os.environ.get(env_key, default))
    except Exception:
        return default


SOFT_DD_PCT = _f("PORTFOLIO_DD_SOFT_PCT", 8.0)
HARD_DD_PCT = _f("PORTFOLIO_DD_HARD_PCT", 15.0)


async def update_and_get(db, account_id: str, equity: float) -> dict:
    """Read+update HWM, return current drawdown snapshot.

    Output:
      {
        "equity": float, "hwm": float, "dd_pct": float,
        "dd_usd": float, "soft_breach": bool, "hard_breach": bool,
        "thresholds": {soft_pct, hard_pct},
        "hwm_updated": bool,           # True if HWM just ratcheted up
        "hwm_at": iso,
      }
    """
    if equity <= 0:
        return {"equity": equity, "hwm": equity, "dd_pct": 0.0, "dd_usd": 0.0,
                "soft_breach": False, "hard_breach": False,
                "thresholds": {"soft_pct": SOFT_DD_PCT, "hard_pct": HARD_DD_PCT},
                "hwm_updated": False, "hwm_at": None}

    doc = await db.accounts.find_one({"_id_str": account_id}) if False else None  # noqa: F841
    # accounts._id is ObjectId; query by string repr via projection
    from bson import ObjectId
    try:
        acc = await db.accounts.find_one({"_id": ObjectId(account_id)})
    except Exception:
        acc = None
    hwm_prev = float((acc or {}).get("equity_hwm") or 0.0)
    hwm_at_prev = (acc or {}).get("equity_hwm_at")

    hwm = max(hwm_prev, equity)
    hwm_updated = hwm > hwm_prev + 1e-6
    now_iso = datetime.now(timezone.utc).isoformat()

    if acc and hwm_updated:
        try:
            await db.accounts.update_one(
                {"_id": acc["_id"]},
                {"$set": {"equity_hwm": hwm, "equity_hwm_at": now_iso}},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("HWM persist failed for %s: %s", account_id, e)

    dd_usd = max(0.0, hwm - equity)
    dd_pct = (dd_usd / hwm * 100.0) if hwm > 0 else 0.0

    return {
        "equity": equity,
        "hwm": hwm,
        "dd_pct": round(dd_pct, 3),
        "dd_usd": round(dd_usd, 2),
        "soft_breach": dd_pct >= SOFT_DD_PCT,
        "hard_breach": dd_pct >= HARD_DD_PCT,
        "thresholds": {"soft_pct": SOFT_DD_PCT, "hard_pct": HARD_DD_PCT},
        "hwm_updated": hwm_updated,
        "hwm_at": now_iso if hwm_updated else hwm_at_prev,
    }
