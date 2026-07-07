"""iter-111 · Adaptive Position Sizing — risk% earned per trade, not fixed.

Final risk = base risk% × Π(component multipliers), where each component is
a bounded multiplier:
  · confidence   — calibrated confidence (iter-110): 60% → ~0.3×, 98% → ~1.8×
  · volatility   — inverse vol targeting: expanded ATR → smaller size
  · accuracy     — rolling 20-trade win rate: cold streak → cut, hot → press
  · liquidity    — order-book / liquidity-draw alignment
  · drawdown     — 30-day realized peak-to-now drawdown vs equity: deep DD
                   throttles hard (capital preservation beats opportunity)
Result clamped to [floor, cap] (default 0.1% … 2.0%).

Example: conf 98 · calm vol · 65% WR · aligned book · no DD  → ~2.0% risk
         conf 60 · HIGH risk tier · cold streak              → ~0.25% risk"""
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

MULT_MIN, MULT_MAX = 0.10, 2.00
DEFAULT_FLOOR_PCT, DEFAULT_CAP_PCT = 0.10, 2.00
ACCURACY_WINDOW = 20
DD_LOOKBACK_DAYS = 30


def confidence_mult(uncertainty: dict | None, raw_conf=None) -> float:
    conf = None
    risk_tier = None
    if uncertainty:
        conf = uncertainty.get("confidence_pct")
        risk_tier = uncertainty.get("risk")
    if conf is None:
        conf = raw_conf
    if conf is None:
        return 1.0
    m = max(0.3, min(1.8, (float(conf) - 50.0) / 30.0))
    if risk_tier == "HIGH":
        m *= 0.7
    elif risk_tier == "LOW":
        m *= 1.1
    return round(max(MULT_MIN, min(1.98, m)), 2)


def volatility_mult(bars: list | None) -> float:
    """Inverse volatility targeting on M15 bars: recent ATR vs session ATR."""
    if not bars or len(bars) < 30:
        return 1.0

    def atr(n):
        trs = [max(bars[i]["h"] - bars[i]["l"],
                   abs(bars[i]["h"] - bars[i - 1]["c"]),
                   abs(bars[i]["l"] - bars[i - 1]["c"]))
               for i in range(max(1, len(bars) - n), len(bars))]
        return sum(trs) / len(trs) if trs else 0.0

    full, recent = atr(len(bars) - 1), atr(8)
    if full <= 0 or recent <= 0:
        return 1.0
    return round(max(0.6, min(1.25, full / recent)), 2)


def accuracy_mult(wins: int, losses: int) -> float:
    n = wins + losses
    if n < 5:
        return 1.0
    wr = wins / n
    if wr < 0.40:
        return 0.5
    if wr < 0.50:
        return 0.75
    if wr < 0.60:
        return 1.0
    if wr < 0.70:
        return 1.15
    return 1.3


def liquidity_mult(lmap: dict | None, action: str) -> float:
    if not lmap or not lmap.get("ready"):
        return 1.0
    m = 1.0
    dom = lmap.get("dom") or {}
    imb = dom.get("imbalance")
    if dom.get("live") and imb is not None and abs(imb) >= 0.25:
        m *= 1.1 if (imb > 0) == (action == "BUY") else 0.8
    draw = lmap.get("draw")
    if draw in ("UP", "DOWN"):
        aligned = (draw == "UP") == (action == "BUY")
        m *= 1.05 if aligned else 0.9
    return round(max(0.7, min(1.15, m)), 2)


def drawdown_mult(pnls: list, equity: float) -> tuple[float, float]:
    """(multiplier, dd_frac). pnls oldest→newest; DD = peak-to-now of the
    cumulative realized curve, relative to current equity."""
    if not pnls or equity <= 0:
        return 1.0, 0.0
    cum = peak = 0.0
    for p in pnls:
        cum += float(p)
        peak = max(peak, cum)
    dd_frac = max(0.0, (peak - cum) / equity)
    if dd_frac >= 0.10:
        return 0.4, round(dd_frac, 3)
    if dd_frac >= 0.05:
        return 0.6, round(dd_frac, 3)
    if dd_frac >= 0.02:
        return 0.8, round(dd_frac, 3)
    return 1.0, round(dd_frac, 3)


def combine(components: dict, base_risk_pct: float,
            floor_pct: float = DEFAULT_FLOOR_PCT,
            cap_pct: float = DEFAULT_CAP_PCT) -> dict:
    mult = 1.0
    for v in components.values():
        mult *= float(v)
    mult = max(MULT_MIN, min(MULT_MAX, mult))
    risk = max(floor_pct, min(cap_pct, base_risk_pct * mult))
    return {"multiplier": round(mult, 3), "risk_pct": round(risk, 3),
            "base_risk_pct": base_risk_pct,
            "components": {k: round(float(v), 2) for k, v in components.items()}}


async def compute_adaptive_risk(db, user_id: str, cfg: dict, signal: dict,
                                account: dict, base_risk_pct: float,
                                account_id=None) -> dict:
    from pip_utils import base_symbol
    action = signal.get("action")
    comps = {"confidence": confidence_mult(signal.get("uncertainty"),
                                           signal.get("confidence"))}

    base = base_symbol(signal.get("symbol") or "")
    cdoc = await db.intraday_candles.find_one(
        {"user_id": user_id, "symbol": base}, {"bars": 1})
    comps["volatility"] = volatility_mult((cdoc or {}).get("bars"))

    q = {"user_id": user_id, "status": "closed", "pnl": {"$ne": None},
         "origin": "auto"}
    if account_id:
        q["account_id"] = account_id
    recent = await db.trades.find(q, {"pnl": 1}).sort(
        "closed_at", -1).limit(ACCURACY_WINDOW).to_list(ACCURACY_WINDOW)
    wins = sum(1 for t in recent if t["pnl"] > 0)
    comps["accuracy"] = accuracy_mult(wins, len(recent) - wins)

    comps["liquidity"] = liquidity_mult(signal.get("liquidity"), action)

    since = (datetime.now(timezone.utc)
             - timedelta(days=DD_LOOKBACK_DAYS)).isoformat()
    dd_trades = await db.trades.find(
        {**q, "closed_at": {"$gte": since}},
        {"pnl": 1, "closed_at": 1}).sort("closed_at", 1).to_list(3000)
    equity = float(account.get("equity") or account.get("balance") or 0)
    comps["drawdown"], dd_frac = drawdown_mult(
        [t["pnl"] for t in dd_trades], equity)

    out = combine(comps, base_risk_pct,
                  float(cfg.get("adaptive_risk_floor_pct") or DEFAULT_FLOOR_PCT),
                  float(cfg.get("adaptive_risk_cap_pct") or DEFAULT_CAP_PCT))
    out["drawdown_frac"] = dd_frac
    out["recent_win_rate"] = round(wins / len(recent), 2) if recent else None
    return out
