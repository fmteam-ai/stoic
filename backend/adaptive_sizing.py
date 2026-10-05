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
# Phase-1 value-driven sizing bounds (user decision): poor trades get less
# capital, excellent trades more — clamped to [0.25%, 1.3%] per trade.
DEFAULT_FLOOR_PCT, DEFAULT_CAP_PCT = 0.25, 1.30
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


def spread_mult(spread_pips: float | None, symbol: str | None) -> float:
    """Wide spread = thin/expensive market — size down automatically."""
    if spread_pips is None or spread_pips <= 0:
        return 1.0
    s = (symbol or "").upper()
    if s.startswith(("XAU", "GOLD")):
        tiers = ((3.5, 1.0), (6.0, 0.85), (12.0, 0.7))
    elif s.startswith(("BTC", "ETH")):
        tiers = ((15.0, 1.0), (30.0, 0.85), (60.0, 0.7))
    elif any(s.startswith(i) for i in ("US30", "US100", "NAS", "GER", "DE40",
                                       "SPX", "US500", "UK100", "JP225")):
        tiers = ((3.0, 1.0), (6.0, 0.85), (12.0, 0.7))
    else:  # FX pairs (pips)
        tiers = ((1.5, 1.0), (3.0, 0.85), (6.0, 0.7))
    for cap, m in tiers:
        if spread_pips <= cap:
            return m
    return 0.55


def regime_mult(regime: str | None) -> float:
    """Market regime scaling: clean trends earn a little more risk,
    ranges a little less, volatility shocks materially less."""
    r = str(regime or "").upper()
    if "SHOCK" in r or "VOLATIL" in r:
        return 0.7
    if r.startswith("TREND"):
        return 1.1
    if r == "RANGE":
        return 0.9
    return 1.0


def regime_certainty_mult(uncertainty: float | None) -> float:
    """Autopilot #1 — reduce exposure when the market classification itself
    is uncertain (normalized regime-probability entropy 0..1)."""
    if uncertainty is None:
        return 1.0
    u = float(uncertainty)
    if u <= 0.55:
        return 1.0
    if u <= 0.70:
        return 0.9
    if u <= 0.85:
        return 0.8
    return 0.65


def exposure_mult(open_auto_count: int, floating_dd_pct: float | None) -> float:
    """Portfolio exposure: the more concurrent open risk, the smaller each
    NEW position. Floating losses shrink new risk further."""
    if open_auto_count <= 2:
        m = 1.0
    elif open_auto_count <= 4:
        m = 0.85
    else:
        m = 0.7
    if floating_dd_pct is not None and floating_dd_pct <= -3.0:
        m *= 0.8
    return round(max(0.5, m), 2)


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
        {"user_id": user_id, "symbol": base, "timeframe": "M15"}, {"bars": 1})
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

    # current spread — thin/expensive market sizes down
    sym_full = signal.get("symbol") or ""
    sp = None
    try:
        sp = (account.get("current_spreads") or {}).get(sym_full)
        if sp is None:
            sp = (account.get("current_spreads") or {}).get(base)
        sp = float(sp) if sp is not None else None
    except Exception:
        sp = None
    comps["spread"] = spread_mult(sp, sym_full)

    # market regime — trend/range/shock scaling
    regime = ((signal.get("regime_execution_mode") or {}).get("regime")
              or cfg.get("_last_regime"))
    comps["regime"] = regime_mult(regime)

    # regime-classification uncertainty — uncertain market = smaller size
    try:
        from market_regime import detect as _regime_detect
        snap = await _regime_detect(db, user_id)
        comps["regime_certainty"] = regime_certainty_mult(
            (snap.get("probabilities") or {}).get("uncertainty"))
    except Exception:  # noqa: BLE001
        comps["regime_certainty"] = 1.0

    # portfolio exposure — concurrent open auto-risk + floating P&L
    open_q = {"user_id": user_id, "status": "open", "origin": "auto"}
    if account_id:
        open_q["account_id"] = account_id
    open_count = await db.trades.count_documents(open_q)
    equity0 = float(account.get("equity") or 0)
    bal0 = float(account.get("balance") or 0)
    floating = (round((equity0 - bal0) / bal0 * 100, 2)
                if bal0 > 0 and account.get("equity") is not None else None)
    comps["exposure"] = exposure_mult(open_count, floating)

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
