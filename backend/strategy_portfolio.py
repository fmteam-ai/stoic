"""Multi-strategy portfolio optimizer (Phase 3).

Professional systems optimize strategies TOGETHER. For every strategy class
(trend / scalp / breakout / mean_reversion / experimental) this module
computes, from the user's REAL closed bot trades:

    expected_return   mean daily P&L ($ and per-trade expectancy)
    volatility        std-dev of daily P&L
    drawdown          max peak-to-trough of the cumulative P&L curve
    correlation       avg positive pairwise correlation of daily P&L vs
                      the other strategies (diversification value)
    capacity          how much size the strategy can absorb — cost share
                      of its edge (spread vs TP distance) + trade frequency
    confidence        sample size × consistency (t-statistic of the mean)

…then allocates the daily risk pool DYNAMICALLY:

    raw_i  = risk-adjusted return (Sharpe⁺) × 1/(1+avg_pos_corr) × capacity
    w_i    = confidence-blend between raw allocation and the static base
             share (low evidence → stay near base; strong evidence → follow
             the data), clamped to [5%, 50%] and renormalized.

Consumed by risk_budget.budget_status (cfg `dynamic_allocation_enabled`,
default ON) — cached 1h per user+account in `strategy_allocations`.
"""
import logging
import math
from datetime import datetime, timedelta, timezone

from risk_budget import DEFAULT_ALLOCATIONS, strategy_class_of

logger = logging.getLogger("strategy-portfolio")

WINDOW_DAYS = 30
CACHE_TTL_SEC = 3600
W_MIN, W_MAX = 0.05, 0.50

ASSET_CLASS = {"XAUUSD": "gold", "XAGUSD": "gold",
               "BTCUSD": "crypto", "ETHUSD": "crypto",
               "US30": "indices", "NAS100": "indices", "SPX500": "indices"}


def _now():
    return datetime.now(timezone.utc)


def _asset_of(symbol: str) -> str:
    from pip_utils import base_symbol
    return ASSET_CLASS.get(base_symbol(symbol), "forex")


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _corr(a, b):
    if len(a) != len(b) or len(a) < 5:
        return None
    ma, mb = _mean(a), _mean(b)
    sa, sb = _std(a), _std(b)
    if sa <= 0 or sb <= 0:
        return None
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) - 1)
    return max(-1.0, min(1.0, cov / (sa * sb)))


def _max_drawdown(daily):
    peak, dd, cum = 0.0, 0.0, 0.0
    for p in daily:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def _capacity(trades: list) -> dict:
    """0-100: how much size this strategy can absorb. Edge that barely
    clears the spread (scalps) has low capacity; wide-target strategies
    absorb size easily. Frequency adds deployable opportunities."""
    from monte_carlo import typical_cost
    from pip_utils import price_to_pips
    cost_shares = []
    for t in trades:
        tp = t.get("tp_pips") or []
        sym = t.get("symbol") or "XAUUSD"
        if not tp or not tp[0]:
            continue
        try:
            cost_pips = price_to_pips(sym, typical_cost(sym, float(t.get("entry_price") or 0)))
            cost_shares.append(min(1.0, cost_pips / float(tp[0])))
        except Exception:
            continue
    cost_share = _mean(cost_shares) if cost_shares else 0.5
    per_day = len(trades) / WINDOW_DAYS
    freq_factor = min(1.0, 0.5 + per_day / 4.0)   # ≥2 trades/day → full
    score = round(100.0 * (1.0 - cost_share) * freq_factor, 1)
    return {"score": score, "cost_share": round(cost_share, 2),
            "trades_per_day": round(per_day, 2),
            "detail": f"cost eats {cost_share * 100:.0f}% of TP1 edge · "
                      f"{per_day:.1f} trades/day"}


def _confidence(daily: list, n_trades: int) -> dict:
    """0-1: sample size × consistency of the mean (t-stat)."""
    active = [d for d in daily if d != 0.0]
    size_f = min(1.0, n_trades / 30.0)
    sd = _std(daily)
    if sd > 0 and len(active) >= 3:
        t = _mean(daily) / (sd / math.sqrt(len(daily)))
        consist = 0.5 + 0.5 * min(1.0, abs(t) / 2.0)
    else:
        consist = 0.5
    conf = round(size_f * consist, 2)
    return {"score": conf, "n_trades": n_trades,
            "active_days": len(active),
            "detail": f"{n_trades} trades over {len(active)} active days"}


async def strategy_metrics(db, user_id: str, account_id: str | None = None,
                           days: int = WINDOW_DAYS) -> dict:
    """The full per-strategy metric table + per-asset breakdown."""
    since = (_now() - timedelta(days=days)).isoformat()
    q = {"user_id": user_id, "status": "closed", "origin": "auto",
         "pnl": {"$ne": None}, "closed_at": {"$gte": since}}
    if account_id:
        q["account_id"] = account_id
    day0 = (_now() - timedelta(days=days - 1)).date()
    dates = [(day0 + timedelta(days=i)).isoformat() for i in range(days)]
    daily = {k: {d: 0.0 for d in dates} for k in DEFAULT_ALLOCATIONS}
    trades_by = {k: [] for k in DEFAULT_ALLOCATIONS}
    assets = {}
    async for t in db.trades.find(
            q, {"pnl": 1, "scope": 1, "strategy_class": 1, "closed_at": 1,
                "symbol": 1, "tp_pips": 1, "entry_price": 1}).limit(5000):
        cls = strategy_class_of(t.get("scope"), t.get("strategy_class"))
        pnl = float(t["pnl"])
        d = str(t.get("closed_at") or "")[:10]
        if d in daily[cls]:
            daily[cls][d] += pnl
        trades_by[cls].append(t)
        a = _asset_of(t.get("symbol") or "")
        arow = assets.setdefault(a, {"trades": 0, "pnl": 0.0, "wins": 0})
        arow["trades"] += 1
        arow["pnl"] = round(arow["pnl"] + pnl, 2)
        arow["wins"] += 1 if pnl > 0 else 0

    series = {k: [daily[k][d] for d in dates] for k in DEFAULT_ALLOCATIONS}
    out = {}
    for k in DEFAULT_ALLOCATIONS:
        s = series[k]
        n = len(trades_by[k])
        mean_d, sd = _mean(s), _std(s)
        corrs = []
        for j in DEFAULT_ALLOCATIONS:
            if j == k or not trades_by[j]:
                continue
            c = _corr(s, series[j])
            if c is not None and c > 0:
                corrs.append(c)
        out[k] = {
            "expected_return_usd_day": round(mean_d, 2),
            "expectancy_usd_trade": round(_mean([float(t["pnl"]) for t in trades_by[k]]), 2) if n else 0.0,
            "volatility_usd_day": round(sd, 2),
            "sharpe_daily": round(mean_d / sd, 2) if sd > 0 else 0.0,
            "max_drawdown_usd": round(_max_drawdown(s), 2),
            "avg_pos_correlation": round(_mean(corrs), 2) if corrs else 0.0,
            "capacity": _capacity(trades_by[k]),
            "confidence": _confidence(s, n),
            "n_trades": n,
        }
    return {"window_days": days, "strategies": out, "by_asset": assets}


def build_allocation(metrics: dict, base: dict | None = None) -> dict:
    """Pure: metric table → normalized dynamic weights with confidence blend."""
    base = base or dict(DEFAULT_ALLOCATIONS)
    raws = {}
    for k, m in metrics.items():
        sharpe_pos = max(0.0, m["sharpe_daily"])
        corr_pen = 1.0 / (1.0 + max(0.0, m["avg_pos_correlation"]))
        cap = max(0.1, m["capacity"]["score"] / 100.0)
        raws[k] = sharpe_pos * corr_pen * cap
    total_raw = sum(raws.values())
    weights = {}
    for k in metrics:
        conf = metrics[k]["confidence"]["score"]
        data_w = raws[k] / total_raw if total_raw > 0 else base.get(k, 0.2)
        w = conf * data_w + (1.0 - conf) * base.get(k, 0.2)
        weights[k] = min(W_MAX, max(W_MIN, w))
    norm = sum(weights.values())
    return {k: round(v / norm, 4) for k, v in weights.items()}


async def current_allocations(db, user_id: str, cfg: dict,
                              account_id: str | None = None) -> dict | None:
    """1h-cached dynamic weights for risk_budget. None → caller falls back
    to static shares."""
    key = {"user_id": user_id, "account_id": account_id}
    cached = await db.strategy_allocations.find_one(key)
    if cached and (_now() - cached["at"].replace(tzinfo=timezone.utc)
                   ).total_seconds() < CACHE_TTL_SEC:
        return {"weights": cached["weights"], "basis": "dynamic",
                "computed_at": cached["at"]}
    m = await strategy_metrics(db, user_id, account_id)
    total_trades = sum(v["n_trades"] for v in m["strategies"].values())
    if total_trades < 10:
        return None  # not enough evidence — static base shares
    base = dict(DEFAULT_ALLOCATIONS)
    try:
        for k, v in (cfg.get("risk_budget_allocations") or {}).items():
            if k in base and 0 < float(v) <= 1:
                base[k] = float(v)
    except Exception:
        pass
    weights = build_allocation(m["strategies"], base)
    await db.strategy_allocations.update_one(
        key, {"$set": {"weights": weights, "at": _now(),
                       "n_trades": total_trades}}, upsert=True)
    return {"weights": weights, "basis": "dynamic", "computed_at": _now()}
