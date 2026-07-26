"""iter-158 — Stress Test mode: synthesizes a sudden market crash and replays
it through the bot's REAL defense layers, producing a per-layer
"stayed calm / panicked" scorecard. No live account state is touched — every
input is synthetic.

Severities:
  mild     -3% drop over 3 bars, spread 3×
  moderate -8% drop over 4 bars, spread 6×
  severe  -15% drop over 5 bars, spread 12×
"""
import time
import uuid
from datetime import datetime, timezone

SEVERITIES = {
    "mild":     {"drop_pct": 3.0,  "crash_bars": 3, "spread_mult": 3.0},
    "moderate": {"drop_pct": 8.0,  "crash_bars": 4, "spread_mult": 6.0},
    "severe":   {"drop_pct": 15.0, "crash_bars": 5, "spread_mult": 12.0},
}


def _build_bars(severity: dict, base: float = 4000.0):
    """40 calm 15m bars, then the crash sequence, then 3 recovery bars."""
    now = time.time()
    calm_range = base * 0.0008              # ~0.08% bars — calm market
    bars = []
    total = 40 + severity["crash_bars"] + 3
    for i in range(40):
        t = now - (total - i) * 900
        bars.append({"t": t, "o": base, "h": base + calm_range / 2,
                     "l": base - calm_range / 2, "c": base})
    price = base
    drop_per_bar = base * severity["drop_pct"] / 100.0 / severity["crash_bars"]
    crash_bars = []
    for i in range(severity["crash_bars"]):
        t = now - (severity["crash_bars"] + 3 - i) * 900
        o = price
        price = price - drop_per_bar
        crash_bars.append({"t": t, "o": o, "h": o + calm_range,
                           "l": price - drop_per_bar * 0.4, "c": price})
    recovery = []
    for i in range(3):
        t = now - (3 - i) * 900
        recovery.append({"t": t, "o": price, "h": price + calm_range / 2,
                         "l": price - calm_range / 2, "c": price})
    return bars, crash_bars, recovery, price


async def run_stress_test(db, severity: str = "moderate",
                          actor: str = "system") -> dict:
    sev = SEVERITIES.get(severity) or SEVERITIES["moderate"]
    calm, crash, recovery, floor_price = _build_bars(sev)
    base = calm[0]["o"]
    checks = []

    def check(layer, calm_result, expect, detail):
        checks.append({"layer": layer, "calm": bool(calm_result),
                       "status": "pass" if calm_result else "fail",
                       "expected": expect, "detail": str(detail)[:400]})

    from risk_engine import abnormal_market_check, drawdown_check
    now_ts = time.time()

    # 1 — volatility shock filter must BLOCK new entries during the crash
    res = abnormal_market_check(calm + crash, now_ts=now_ts)
    check("volatility_shock_filter", res.get("status") == "block",
          "block new entries during the crash", res.get("detail"))

    # 2 — spread blow-out guard: crash spread must exceed the dynamic limit
    from scalp.costs import dynamic_spread_limit

    class _S:  # minimal state/cfg stubs matching the real signature
        spreads = [1.2] * 60  # calm session: p75 ≈ 1.2 pips

    class _C:
        max_spread_pips = 3.0
    limit = dynamic_spread_limit(_S(), _C())
    crash_spread = 1.2 * sev["spread_mult"]
    check("spread_guard", crash_spread > limit,
          f"crash spread {crash_spread:.1f}p rejected by limit {limit:.1f}p",
          f"dynamic limit {limit:.1f} pips vs crash spread "
          f"{crash_spread:.1f} pips — order would be vetoed")

    # 3 — drawdown circuit breaker: equity hit from the drop must trip the
    # daily limit exactly when it should
    equity = 10_000.0
    exposure = 0.5  # 50% notional exposure through the crash
    pnl_day = -equity * exposure * sev["drop_pct"] / 100.0
    dd = drawdown_check(pnl_day, pnl_day, pnl_day, equity)
    dd_pct = -100.0 * pnl_day / equity
    should_trip = dd_pct >= 3.0  # daily limit
    tripped = dd.get("status") == "block"
    check("drawdown_circuit_breaker", tripped == should_trip,
          f"{dd_pct:.1f}% daily DD {'trips' if should_trip else 'stays under'}"
          " the 3% breaker", dd.get("detail"))

    # 4 — SafetyGuardian sizing floor: position size must COLLAPSE (not grow)
    # as stop distance explodes in the crash
    from risk import compute_position_size
    calm_sl_pips, crash_sl_pips = 30.0, 30.0 * sev["spread_mult"]
    lot_calm = compute_position_size(equity, 0.5, calm_sl_pips)
    lot_crash = compute_position_size(equity, 0.5, crash_sl_pips)
    check("position_sizing_floor", 0 < lot_crash < lot_calm,
          "size shrinks as volatility widens the stop",
          f"calm lot {lot_calm} → crash lot {lot_crash} "
          f"(risk held constant at 0.5%)")

    # 5 — recovery: once the market calms, the filter must stand down and
    # trading resumes (no stuck state)
    post = calm[5:] + crash + recovery  # rolling window with calm tail
    res_after = abnormal_market_check(post, now_ts=now_ts)
    calm_again = res_after.get("status") in ("ok", "trim")
    check("post_crash_recovery", calm_again,
          "filter stands down after conditions normalize",
          res_after.get("detail"))

    passed = sum(1 for c in checks if c["status"] == "pass")
    doc = {"run_id": f"stress-{uuid.uuid4().hex[:10]}",
           "severity": severity,
           "params": {**sev, "base_price": base,
                      "floor_price": round(floor_price, 2)},
           "checks": checks,
           "passed": passed, "failed": len(checks) - passed,
           "verdict": "STAYED_CALM" if passed == len(checks) else "PANICKED",
           "started_by": actor,
           "at": datetime.now(timezone.utc)}
    await db.stress_tests.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc
