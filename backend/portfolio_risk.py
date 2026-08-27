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


def regime_correlation(a: str, b: str, vol_stress: float = 0.0) -> float:
    """v60 #12 — correlations are regime-sensitive, not static: in a
    volatility shock everything correlates toward ±1."""
    c = pair_correlation(a, b)
    try:
        vs = max(0.0, min(1.0, float(vol_stress or 0)))
    except (TypeError, ValueError):
        vs = 0.0
    if c == 0.0 or vs == 0.0:
        return c
    sign = 1.0 if c > 0 else -1.0
    return round(sign * min(1.0, abs(c) + 0.35 * vs), 3)


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
             params: dict | None = None,
             vol_stress: float = 0.0) -> dict:
    """Portfolio verdict for ONE new candidate against ALL open positions.
    Returns ok/blocks plus the full metric set for explainability.
    vol_stress (0-1) tightens correlations toward ±1 (v60 regime-aware)."""
    cfg = {**DEFAULTS, **(params or {})}
    equity = max(float(equity or 0), 0.01)
    blocks: list = []

    risk_c = position_risk_usd(candidate)

    def _corr(p) -> float:
        c = position_correlation(candidate, p)
        if vol_stress and c:
            sign = 1.0 if c > 0 else -1.0
            c = sign * min(1.0, abs(c) + 0.35 * min(1.0, vol_stress))
        return c

    cluster = risk_c + sum(
        max(0.0, _corr(p)) * position_risk_usd(p)
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

    # v60 — marginal FACTOR contribution (USD/GOLD/EQUITIES/CRYPTO/…)
    fv = marginal_factor_verdict(positions, candidate, equity)
    blocks.extend(fv["breaches"])

    return {"ok": not blocks, "blocks": blocks,
            "candidate_risk_usd": round(risk_c, 2),
            "cluster_risk_usd": round(cluster, 2),
            "cluster_cap_usd": round(cluster_cap, 2),
            "currency_exposure": exp,
            "currency_cap_usd": round(ccy_cap, 2),
            "stress": {**stress, "cap_usd": round(stress_cap, 2)},
            "factor_verdict": fv,
            "vol_stress": round(min(1.0, max(0.0, vol_stress)), 3),
            "open_positions": len(positions),
            "correlations": correlation_matrix(everything)}


# ─────────────── Portfolio Risk Brain 2.0 (v60 #12) — factor model ────────

FACTOR_CAP_PCT = 2.5
GROUP_FACTORS = {
    "metals": {"GOLD": 1.0, "USD": -0.6, "RISK_OFF": 0.4},
    "indices": {"EQUITIES": 1.0, "RISK_ON": 0.6},
    "crypto": {"CRYPTO": 1.0, "RISK_ON": 0.5},
}


def factor_loadings(symbol: str) -> dict:
    """Deterministic factor loadings per symbol (v60 factor model)."""
    s = base_symbol(symbol).upper()
    g = group_of(s)
    if g in GROUP_FACTORS:
        return dict(GROUP_FACTORS[g])
    b, q = legs(s)
    return {b: 1.0, q: -1.0}


def factor_risk(positions: list) -> dict:
    """Signed risk-USD per FACTOR (currency legs + macro factors)."""
    out: dict = {}
    for p in positions:
        r = position_risk_usd(p) * _dir(p)
        for f, load in factor_loadings(p.get("symbol") or "").items():
            out[f] = round(out.get(f, 0.0) + r * load, 2)
    return out


def marginal_factor_verdict(positions: list, candidate: dict,
                            equity: float,
                            cap_pct: float = FACTOR_CAP_PCT) -> dict:
    """Marginal factor contribution of ONE candidate — which factor does
    this trade really add, and does it breach the factor budget?"""
    equity = max(float(equity or 0), 0.01)
    before = factor_risk(positions)
    after = factor_risk(list(positions) + [candidate])
    cap = equity * cap_pct / 100.0
    breaches = []
    marginal = {}
    for f in set(before) | set(after):
        delta = round(after.get(f, 0.0) - before.get(f, 0.0), 2)
        if delta:
            marginal[f] = delta
        if abs(after.get(f, 0.0)) > cap \
                and abs(after.get(f, 0.0)) > abs(before.get(f, 0.0)):
            breaches.append(
                f"factor {f} exposure ${abs(after[f]):.0f} exceeds "
                f"{cap_pct}% of equity")
    fraction = 1.0
    if breaches:
        worst_over = max(
            (abs(after[f]) - cap) for f in after
            if abs(after.get(f, 0.0)) > cap
            and abs(after.get(f, 0.0)) > abs(before.get(f, 0.0)))
        cand_r = position_risk_usd(candidate) or 1e-9
        fraction = round(max(0.0, 1.0 - worst_over / cand_r), 3)
    return {"marginal_factors": marginal, "factor_risk_after": after,
            "factor_cap_usd": round(cap, 2), "breaches": breaches,
            "approved_fraction": fraction}


def factor_exposure(positions: list, equity: float) -> dict:
    """Real-time factor view: risk-weighted currency factors (the four-
    trades-that-are-all-USD problem), directionality and concentration."""
    equity = max(float(equity or 0), 0.01)
    ccy = currency_exposure(positions)
    total_risk = sum(position_risk_usd(p) for p in positions)
    factors = [{"factor": c, "net_risk_usd": round(v, 2),
                "pct_of_equity": round(abs(v) / equity * 100, 2),
                "direction": "LONG" if v > 0 else "SHORT"}
               for c, v in sorted(ccy.items(),
                                  key=lambda kv: -abs(kv[1])) if v]
    dominant = factors[0] if factors else None
    return {"factors": factors, "dominant_factor": dominant,
            "total_risk_usd": round(total_risk, 2),
            "concentration": round(abs(dominant["net_risk_usd"])
                                   / total_risk, 2)
            if dominant and total_risk else 0.0}


async def marginal_verdict(db, account_id: str, candidate: dict,
                           equity: float, user_id: str | None = None,
                           params: dict | None = None,
                           vol_stress: float = 0.0) -> dict:
    """Portfolio Risk Brain — what risk does THIS trade add to the whole
    portfolio? APPROVE / REDUCE / REJECT with an approved risk fraction;
    reductions are recorded for empirical verdict-outcome scoring."""
    positions = await open_positions(db, account_id, user_id=user_id)
    ev = evaluate(positions, candidate, equity, params,
                  vol_stress=vol_stress)
    risk_c = ev["candidate_risk_usd"]
    if ev["ok"]:
        verdict = {"verdict": "APPROVE", "approved_fraction": 1.0,
                   "blocks": [],
                   "marginal_cluster_risk_usd": round(
                       ev["cluster_risk_usd"], 2),
                   "factors": factor_exposure(positions + [candidate],
                                              equity)}
        return verdict
    # scale the candidate until the correlated cluster fits its cap
    headroom = max(0.0, ev["cluster_cap_usd"]
                   - (ev["cluster_risk_usd"] - risk_c))
    fraction = round(min(1.0, headroom / risk_c), 3) if risk_c else 0.0
    # v60 — the factor budget can only shrink the approval further
    fraction = min(fraction,
                   float(ev["factor_verdict"]["approved_fraction"]))
    verdict_name = "REJECT" if fraction < 0.2 else "REDUCE"
    out = {"verdict": verdict_name,
           "approved_fraction": 0.0 if verdict_name == "REJECT"
           else fraction,
           "blocks": ev["blocks"],
           "marginal_cluster_risk_usd": round(ev["cluster_risk_usd"], 2),
           "cluster_cap_usd": ev["cluster_cap_usd"],
           "factors": factor_exposure(positions + [candidate], equity)}
    try:
        from verdict_tracking import record_verdict
        out["verdict_tracking_id"] = await record_verdict(
            db, source="portfolio_brain", verdict=verdict_name,
            requested=float(candidate.get("lot") or 0),
            approved=round(float(candidate.get("lot") or 0)
                           * out["approved_fraction"], 2),
            unit="lot", user_id=user_id,
            limiting_factor="portfolio_" + ("cluster" if any(
                "cluster" in b for b in ev["blocks"]) else "factor"),
            reasons=ev["blocks"],
            context={"symbol": candidate.get("symbol"),
                     "side": candidate.get("action"),
                     "entry_price": candidate.get("entry_price"),
                     "stop_loss": candidate.get("stop_loss")})
    except Exception:
        pass
    return out


async def open_positions(db, account_id: str, limit: int = 100,
                         user_id: str | None = None) -> list:
    """All live commitments on the account, EVERY scope (cross-strategy).
    Pass user_id to enforce ownership scoping at the query level."""
    q = {"account_id": account_id, "status": {"$in": ["open", "pending"]}}
    if user_id:
        q["user_id"] = user_id
    out = []
    async for tr in db.trades.find(
            q,
            {"symbol": 1, "action": 1, "lot_size": 1, "entry_price": 1,
             "stop_loss": 1, "scope": 1}).limit(limit):
        out.append({"symbol": tr.get("symbol"), "action": tr.get("action"),
                    "lot": tr.get("lot_size"),
                    "entry_price": tr.get("entry_price"),
                    "stop_loss": tr.get("stop_loss"),
                    "scope": tr.get("scope") or "bot"})
    return out


async def snapshot(db, account_id: str, equity: float,
                   user_id: str | None = None) -> dict:
    """Observability endpoint payload: the portfolio picture with no
    candidate — exposure, stress and pairwise correlations as they stand."""
    positions = await open_positions(db, account_id, user_id=user_id)
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
