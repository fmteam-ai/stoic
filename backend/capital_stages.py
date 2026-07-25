"""Phase 3.1 — Progressive capital scaling.

Capital authority grows on EVIDENCE, never elapsed time:
  Stage 1 PILOT   — cap 0.25% risk/trade (entry state)
  Stage 2 SCALE   — cap 0.50% after ≥100 trades, PF ≥ 1.05, DD ≤ 20R
  Stage 3 DEPLOY  — config risk honored after ≥300 trades, CI-lower > 0,
                    DD ≤ 40R (same bar as autonomous promotion)
The cap is enforced in the live sizing path (bot_runner) after adaptive
sizing — it can only lower risk, never raise it.
"""
import math
import time
from datetime import datetime, timedelta, timezone

_cache: dict = {}
CACHE_TTL = 15 * 60


async def capital_stage(db, user_id: str, days: int = 90) -> dict:
    from shadow_benchmark import _executed_rs
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rs = [t["r"] for t in await _executed_rs(db, user_id, since)]
    n = len(rs)
    mean = sum(rs) / n if n else 0.0
    sem = (math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1) / n)
           if n > 1 else 0.0)
    ci_lower = mean - 1.96 * sem
    wins = sum(r for r in rs if r > 0)
    losses = abs(sum(r for r in rs if r < 0))
    pf = wins / losses if losses else None
    equity = peak = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)

    if n >= 300 and ci_lower > 0 and dd <= 40:
        stage, label, cap = 3, "DEPLOY", None
        nxt = "full config risk honored — maintain the metrics"
    elif n >= 100 and (pf is None or pf >= 1.05) and dd <= 20:
        stage, label, cap = 2, "SCALE", 0.5
        nxt = (f"stage 3 at ≥300 trades ({n}), CI-lower > 0 "
               f"({ci_lower:+.3f}R), DD ≤ 40R ({dd:.1f}R)")
    else:
        stage, label, cap = 1, "PILOT", 0.25
        nxt = (f"stage 2 at ≥100 trades ({n}), PF ≥ 1.05 "
               f"({round(pf, 2) if pf else 'n/a'}), DD ≤ 20R ({dd:.1f}R)")
    return {"stage": stage, "label": label, "risk_cap_pct": cap,
            "evidence": {"n": n, "mean_r": round(mean, 4),
                         "ci95_lower_r": round(ci_lower, 4),
                         "profit_factor": round(pf, 2) if pf else None,
                         "max_drawdown_r": round(dd, 2), "days": days},
            "next_milestone": nxt}


async def stage_risk_cap(db, user_id: str) -> tuple:
    now = time.time()
    cached = _cache.get(user_id)
    if cached and cached[0] > now:
        info = cached[1]
    else:
        info = await capital_stage(db, user_id)
        _cache[user_id] = (now + CACHE_TTL, info)
    return info.get("risk_cap_pct"), info
