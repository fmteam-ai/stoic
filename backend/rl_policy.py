"""iter-61 · Offline RL policy layer.

Learns from the user's REAL logged trades (no simulator) with the objective
    maximize  E[ PnL − λ·loss − μ·drawdown_deepening ]
Distributional Q-values per discrete state (symbol|direction|session|regime|
short-tier-alignment) with count-based confidence: a state needs MIN_VISITS
before the policy may act on it, and BLOCK requires the 80% upper confidence
bound of the reward to be negative.

Modes (per bot config `rl_policy_mode`):
  off       — gate disabled
  advisory  — decisions annotated on signals + counters only (default)
  enforce   — BLOCK skips the trade, SCALE halves the lot
"""
import logging
from datetime import datetime, timedelta, timezone

from pip_utils import base_symbol

logger = logging.getLogger(__name__)

RISK_LAMBDA = 0.5      # loss aversion: a $1 loss costs $1.50 of reward
DD_MU = 0.5            # penalty per $ of new drawdown depth created
MIN_VISITS = 8
Z = 1.28               # ~80% one-sided CI
SCALE_FACTOR = 0.5
LOOKBACK_DAYS = 90
POLICY_TTL_HOURS = 6


def _session_of(sig: dict) -> str:
    s = sig.get("session")
    if isinstance(s, dict):
        return str(s.get("primary") or "unknown").lower()
    return str(s or "unknown").lower()


def _regime_of(sig: dict) -> str:
    r = sig.get("regime")
    if isinstance(r, dict):
        return str(r.get("regime") or "unknown").upper()
    return str(r or "unknown").upper()


def _short_alignment(action: str, sig: dict) -> str:
    d = (((sig.get("mtf_tiers") or {}).get("SHORT")) or {}).get("direction")
    if d not in ("UP", "DOWN"):
        return "FLAT"
    if (action == "BUY") == (d == "UP"):
        return "WITH"
    return "AGAINST"


def extract_state(symbol: str, action: str, sig: dict) -> str:
    return "|".join([
        base_symbol(symbol or "?"),
        str(action or "?"),
        _session_of(sig or {}),
        _regime_of(sig or {}),
        f"short:{_short_alignment(action, sig or {})}",
    ])


def compute_rewards(trades: list) -> list:
    """trades sorted by closed_at → [(trade, reward)] with drawdown tracking."""
    equity = peak = 0.0
    out = []
    for t in trades:
        pnl = float(t.get("pnl") or 0)
        dd_before = max(0.0, peak - equity)
        equity += pnl
        peak = max(peak, equity)
        dd_inc = max(0.0, max(0.0, peak - equity) - dd_before)
        reward = pnl - RISK_LAMBDA * max(0.0, -pnl) - DD_MU * dd_inc
        out.append((t, reward))
    return out


def build_policy(trades: list, signals_by_id: dict) -> dict:
    ordered = sorted(trades, key=lambda t: str(t.get("closed_at") or ""))
    stats: dict = {}
    for t, r in compute_rewards(ordered):
        sig = signals_by_id.get(str(t.get("signal_id") or "")) or {}
        key = extract_state(t.get("symbol"), t.get("action"), sig)
        st = stats.setdefault(key, {"n": 0, "sum": 0.0, "sumsq": 0.0})
        st["n"] += 1
        st["sum"] += r
        st["sumsq"] += r * r
    states = {}
    for k, st in stats.items():
        n = st["n"]
        mean = st["sum"] / n
        var = max(0.0, st["sumsq"] / n - mean * mean)
        sem = (var / n) ** 0.5
        states[k] = {"n": n, "mean": round(mean, 2), "sem": round(sem, 2),
                     "total_reward": round(st["sum"], 2)}
    return {
        "states": states,
        "trades_used": len(ordered),
        "params": {"risk_lambda": RISK_LAMBDA, "dd_mu": DD_MU,
                   "min_visits": MIN_VISITS, "z": Z,
                   "lookback_days": LOOKBACK_DAYS},
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }


def decide(policy: dict, state_key: str) -> dict:
    st = ((policy or {}).get("states") or {}).get(state_key)
    base = {"state": state_key, "n": (st or {}).get("n", 0),
            "mean": (st or {}).get("mean"), "sem": (st or {}).get("sem")}
    if not st or st["n"] < MIN_VISITS:
        return {**base, "decision": "ALLOW",
                "reason": f"insufficient data ({base['n']}/{MIN_VISITS} visits) — neutral"}
    ucb = st["mean"] + Z * st["sem"]
    if ucb < 0:
        return {**base, "decision": "BLOCK",
                "reason": (f"learned reward ${st['mean']:.2f}/trade over {st['n']} trades "
                           f"(80% UCB ${ucb:.2f} < 0) — this setup reliably loses "
                           f"risk-adjusted money")}
    if st["mean"] < 0:
        return {**base, "decision": "SCALE", "scale": SCALE_FACTOR,
                "reason": (f"learned reward ${st['mean']:.2f}/trade over {st['n']} trades "
                           f"(negative but not conclusive) — half size")}
    return {**base, "decision": "ALLOW",
            "reason": f"learned reward +${st['mean']:.2f}/trade over {st['n']} trades"}


def rl_decision(policy: dict, signal: dict, symbol: str) -> dict:
    return decide(policy, extract_state(symbol, signal.get("action"), signal))


async def train_policy(db, user_id: str) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    trades = await db.trades.find({
        "user_id": user_id, "status": "closed", "pnl": {"$ne": None},
        "origin": "auto", "closed_at": {"$gte": since},
        "pnl_estimated": {"$ne": True}, "pnl_unknown": {"$ne": True}, "stats_excluded": {"$ne": True},
    }).to_list(5000)
    from bson import ObjectId
    sids = []
    for t in trades:
        try:
            sids.append(ObjectId(t["signal_id"]))
        except Exception:
            continue
    sigs = {}
    if sids:
        async for s in db.signals.find(
                {"_id": {"$in": sids}},
                {"session": 1, "regime": 1, "mtf_tiers": 1}):
            sigs[str(s["_id"])] = s
    policy = build_policy(trades, sigs)
    policy["user_id"] = user_id
    await db.rl_policies.update_one(
        {"user_id": user_id}, {"$set": policy}, upsert=True)
    logger.info("RL policy trained user=%s trades=%s states=%s",
                user_id, policy["trades_used"], len(policy["states"]))
    return policy


async def get_policy(db, user_id: str) -> dict:
    doc = await db.rl_policies.find_one({"user_id": user_id})
    if doc:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(doc["trained_at"])).total_seconds()
            if age < POLICY_TTL_HOURS * 3600:
                return doc
        except (KeyError, ValueError):
            pass
    return await train_policy(db, user_id)
