"""Phase D · Portfolio Risk — decisions at the PORTFOLIO level across all
open positions (every scope: main bot + scalp fast path), not per-trade.

Deterministic and pure at the core:
  · correlation matrix — currency-leg decomposition + asset-group priors
  · currency exposure — net signed risk-USD per currency leg, capped
  · correlated-cluster cap — a new entry's risk plus its positively
    correlated open risk must fit the cluster budget
  · stress scenarios — per-leg adverse shock with a gap multiplier
    (stops can be jumped), worst leg capped vs equity
  · volatility-adjusted allocation — DOWNSCALE-ONLY size multiplier when
    current volatility runs above its own baseline

Everything here only ever BLOCKS or SHRINKS a new commitment. It can never
increase exposure — the per-trade risk engine remains the ceiling.
"""
import logging

from pip_utils import base_symbol, pip_size, pip_value_usd_per_lot_strict

logger = logging.getLogger("portfolio_risk")

DEFAULTS = {
    "cluster_risk_pct": 1.5,     # candidate + positively-correlated open risk
    "ccy_risk_pct": 2.0,         # net risk per currency leg
    "stress_pct": 5.0,           # worst single-leg shock loss
    "gap_mult": 2.0,             # stops can gap: worst case = 2× stop risk
}

GROUPS = {
    "metals": {"XAUUSD", "XAGUSD"},
    "indices": {"US30", "US100", "NAS100", "US500", "SPX500", "GER40",
                "DAX40", "UK100", "JPN225", "DJ30"},
    "crypto": {"BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "LTCUSD"},
}


def group_of(symbol: str) -> str | None:
    s = base_symbol(symbol).upper()
    for g, members in GROUPS.items():
        if s in members:
            return g
    return None


def legs(symbol: str) -> tuple[str, str]:
    """(base, quote) currency legs. Metals/crypto quote USD; an index is its
    own risk leg quoted in USD."""
    s = base_symbol(symbol).upper()
    g = group_of(s)
    if g == "indices":
        return (s, "USD")
    if g in ("metals", "crypto"):
        return (s[:3], "USD")
    if len(s) == 6 and s.isalpha():
        return (s[:3], s[3:])
    return (s, "USD")


def pair_correlation(a: str, b: str) -> float:
    """Deterministic prior correlation between two SYMBOLS (not positions)."""
    a = base_symbol(a).upper()
    b = base_symbol(b).upper()
    if a == b:
        return 1.0
    ga, gb = group_of(a), group_of(b)
    if ga is not None and ga == gb:
        return 0.85
    (b1, q1), (b2, q2) = legs(a), legs(b)
    if b1 == b2 and q1 == q2:
        return 1.0
    same = (b1 == b2) or (q1 == q2)      # EURUSD ~ GBPUSD (shared quote)
    cross = (b1 == q2) or (q1 == b2)     # EURUSD ~ USDCHF (USD flips side)
    if same:
        return 0.65
    if cross:
        return -0.65
    return 0.0


def _dir(pos: dict) -> float:
    return 1.0 if str(pos.get("direction")
                      or pos.get("action") or "BUY").upper() == "BUY" else -1.0


def position_correlation(pa: dict, pb: dict) -> float:
    """Correlation between two POSITIONS: symbol prior × direction signs
    (long EURUSD and short USDCHF are the SAME macro bet)."""
    return pair_correlation(pa["symbol"], pb["symbol"]) * _dir(pa) * _dir(pb)


def position_risk_usd(pos: dict) -> float:
    """Stop-distance risk of one position in USD (the money truly at stake)."""
    sym = base_symbol(pos.get("symbol") or "")
    ps = pip_size(sym)
    pv = pip_value_usd_per_lot_strict(sym) or 10.0
    lot = float(pos.get("lot") or pos.get("lot_size") or 0)
    entry = pos.get("entry_price")
    stop = pos.get("stop_loss")
    if entry and stop and ps > 0:
        return abs(float(entry) - float(stop)) / ps * pv * lot
    return 10.0 * pv * lot               # unknown stop → assume 10 pips


def correlation_matrix(positions: list) -> dict:
    out = {}
    for i, pa in enumerate(positions):
        for pb in positions[i + 1:]:
            key = f"{base_symbol(pa['symbol'])}~{base_symbol(pb['symbol'])}"
            out[key] = round(position_correlation(pa, pb), 3)
    return out


def currency_exposure(positions: list) -> dict:
    """Net signed risk-USD per currency leg (+ = long that currency)."""
    exp: dict = {}
    for p in positions:
        r = position_risk_usd(p) * _dir(p)
        b, q = legs(p.get("symbol") or "")
        exp[b] = round(exp.get(b, 0.0) + r, 2)
        exp[q] = round(exp.get(q, 0.0) - r, 2)
    return exp


def stress_loss_usd(positions: list, gap_mult: float) -> dict:
    """Worst single-leg adverse shock: for each currency leg take the larger
    of the long-side or short-side gross risk, gap-multiplied (stops can be
    jumped in a shock, correlations snap to 1 within the leg)."""
    long_side: dict = {}
    short_side: dict = {}
    for p in positions:
        r = position_risk_usd(p)
        d = _dir(p)
        b, q = legs(p.get("symbol") or "")
        for leg, sign in ((b, d), (q, -d)):
            side = long_side if sign > 0 else short_side
            side[leg] = side.get(leg, 0.0) + r
    worst_leg, worst = None, 0.0
    for leg in set(long_side) | set(short_side):
        adverse = max(long_side.get(leg, 0.0), short_side.get(leg, 0.0))
        if adverse > worst:
            worst_leg, worst = leg, adverse
    return {"worst_leg": worst_leg,
            "loss_usd": round(worst * gap_mult, 2)}


def vol_size_multiplier(current_vol: float | None,
                        baseline_vol: float | None) -> float:
    """Phase D — volatility-adjusted allocation, DOWNSCALE-ONLY: when the
    symbol runs hotter than its own baseline the new allocation shrinks
    proportionally (floor 0.5); calm markets never earn extra size."""
    try:
        cur = float(current_vol or 0)
        base = float(baseline_vol or 0)
    except (TypeError, ValueError):
        return 1.0
    if cur <= 0 or base <= 0 or cur <= base:
        return 1.0
    return round(max(0.5, base / cur), 3)


def evaluate(positions: list, candidate: dict, equity: float,
             params: dict | None = None) -> dict:
    """Portfolio verdict for ONE new candidate against ALL open positions.
    Returns ok/blocks plus the full metric set for explainability."""
    cfg = {**DEFAULTS, **(params or {})}
    equity = max(float(equity or 0), 0.01)
    blocks: list = []

    risk_c = position_risk_usd(candidate)
    cluster = risk_c + sum(
        max(0.0, position_correlation(candidate, p)) * position_risk_usd(p)
        for p in positions)
    cluster_cap = equity * cfg["cluster_risk_pct"] / 100.0
    if cluster > cluster_cap:
        blocks.append(f"correlated cluster risk ${cluster:.0f} exceeds "
                      f"{cfg['cluster_risk_pct']}% of equity")

    everything = list(positions) + [candidate]
    exp = currency_exposure(everything)
    ccy_cap = equity * cfg["ccy_risk_pct"] / 100.0
    breached = {c: v for c, v in exp.items() if abs(v) > ccy_cap}
    if breached:
        worst = max(breached, key=lambda c: abs(breached[c]))
        blocks.append(f"currency exposure {worst} ${abs(breached[worst]):.0f} "
                      f"exceeds {cfg['ccy_risk_pct']}% of equity")

    stress = stress_loss_usd(everything, cfg["gap_mult"])
    stress_cap = equity * cfg["stress_pct"] / 100.0
    if stress["loss_usd"] > stress_cap:
        blocks.append(f"stress scenario on {stress['worst_leg']} "
                      f"${stress['loss_usd']:.0f} exceeds "
                      f"{cfg['stress_pct']}% of equity")

    return {"ok": not blocks, "blocks": blocks,
            "candidate_risk_usd": round(risk_c, 2),
            "cluster_risk_usd": round(cluster, 2),
            "cluster_cap_usd": round(cluster_cap, 2),
            "currency_exposure": exp,
            "currency_cap_usd": round(ccy_cap, 2),
            "stress": {**stress, "cap_usd": round(stress_cap, 2)},
            "open_positions": len(positions),
            "correlations": correlation_matrix(everything)}


async def open_positions(db, account_id: str, limit: int = 100) -> list:
    """All live commitments on the account, EVERY scope (cross-strategy)."""
    out = []
    async for tr in db.trades.find(
            {"account_id": account_id, "status": {"$in": ["open", "pending"]}},
            {"symbol": 1, "action": 1, "lot_size": 1, "entry_price": 1,
             "stop_loss": 1, "scope": 1}).limit(limit):
        out.append({"symbol": tr.get("symbol"), "action": tr.get("action"),
                    "lot": tr.get("lot_size"),
                    "entry_price": tr.get("entry_price"),
                    "stop_loss": tr.get("stop_loss"),
                    "scope": tr.get("scope") or "bot"})
    return out


async def snapshot(db, account_id: str, equity: float) -> dict:
    """Observability endpoint payload: the portfolio picture with no
    candidate — exposure, stress and pairwise correlations as they stand."""
    positions = await open_positions(db, account_id)
    exp = currency_exposure(positions)
    stress = stress_loss_usd(positions, DEFAULTS["gap_mult"])
    return {"account_id": account_id, "equity": equity,
            "open_positions": positions,
            "currency_exposure": exp,
            "stress": stress,
            "total_risk_usd": round(sum(position_risk_usd(p)
                                        for p in positions), 2),
            "correlations": correlation_matrix(positions),
            "limits": DEFAULTS}
