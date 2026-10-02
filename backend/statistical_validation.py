"""Phase 2.4 — Statistical validation for autonomous promotion.

Promotion to autonomous_live requires EVIDENCE, expressed with confidence
intervals rather than single averages:
  • ≥ MIN_TRADES executed trades in the window
  • positive expectancy after costs at the 95% CI LOWER BOUND
  • drawdown within budget (R terms)
  • stable calibration (MAE ≤ MAX_MAE)
"""
import math
from datetime import datetime, timedelta, timezone

MIN_TRADES = 300
MAX_MAE = 12.0
MAX_DD_R = 40.0


async def promotion_evidence(db, user_id: str, days: int = 90) -> dict:
    from shadow_benchmark import _executed_rs
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rs = [t["r"] for t in await _executed_rs(db, user_id, since)]
    n = len(rs)
    mean = sum(rs) / n if n else 0.0
    if n > 1:
        var = sum((r - mean) ** 2 for r in rs) / (n - 1)
        sem = math.sqrt(var / n)
    else:
        sem = 0.0
    ci_lower = round(mean - 1.96 * sem, 4)
    equity = peak = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)

    mae = None
    try:
        from calibration import compute_calibration
        table = await compute_calibration(db, user_id, days=days)
        tot = err = 0.0
        for ent in table.values():
            for b in ent["buckets"]:
                tot += b["n"]
                err += abs(b["gap"]) * b["n"]
        mae = round(err / tot, 1) if tot else None
    except Exception:  # noqa: BLE001
        mae = None

    checks = {
        "sample_size": {"pass": n >= MIN_TRADES,
                        "detail": f"{n}/{MIN_TRADES} executed trades ({days}d)"},
        "expectancy_ci": {"pass": n >= 2 and ci_lower > 0,
                          "detail": f"mean {mean:+.3f}R, 95% CI lower bound "
                                    f"{ci_lower:+.3f}R (must be > 0)"},
        "drawdown": {"pass": dd <= MAX_DD_R,
                     "detail": f"max drawdown {dd:.1f}R (budget {MAX_DD_R}R)"},
        "calibration": {"pass": mae is not None and mae <= MAX_MAE,
                        "detail": f"calibration MAE "
                                  f"{mae if mae is not None else 'n/a'}pts "
                                  f"(max {MAX_MAE})"},
    }
    blockers = [f"statistical validation: {c['detail']}"
                for c in checks.values() if not c["pass"]]
    return {"days": days, "n": n, "mean_r": round(mean, 4),
            "ci95_lower_r": ci_lower, "max_drawdown_r": round(dd, 2),
            "calibration_mae": mae, "checks": checks,
            "sufficient": not blockers, "blockers": blockers}
