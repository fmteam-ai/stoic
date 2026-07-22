"""Phase C · Meta strategy selector — chooses HOW the one pullback setup is
parameterised per market regime, from realised per-preset performance.

Bandit-lite: recency-weighted mean net pips per (preset, regime) with a UCB
exploration bonus; presets below the sample floor keep the safe default.
Slow-path cached (like permissions) — the tick path reads memory only.
"""
import asyncio
import logging
import math
import time

logger = logging.getLogger("scalp.strategy_select")

# Parameter presets for scalp.setup.detect() — SAFE variations only: they
# tune selectivity of the SAME setup, never invent new exposure.
PRESETS: dict = {
    "pullback_default": {},                       # setup.DEFAULTS as-is
    "pullback_strict": {"impulse_vol_mult": 2.2, "pullback_min_frac": 0.20,
                        "pullback_max_frac": 0.50, "resume_min_pips": 0.15},
    "pullback_loose": {"impulse_min_pips": 1.2, "impulse_vol_mult": 1.5,
                       "pullback_max_frac": 0.65},
}
DEFAULT_PRESET = "pullback_default"
MIN_SAMPLES = 10                 # below this a preset stays exploratory
RECENCY_HALF_LIFE = 100          # decisions; weight = 0.5 ** (age/half_life)
UCB_C = 1.0
REFRESH_SEC = 300
STALE_SEC = 900

_cache: dict = {}                # symbol -> {"ts", "by_regime", "stats"}
_inflight: set = set()


def score_presets(rows: list) -> dict:
    """rows: chronological [{preset, regime, net_pips}]. Returns
    {regime: {"preset": best, "table": {preset: {n, weighted_mean, ucb}}}}."""
    by: dict = {}
    n_total = max(1, len(rows))
    for age, row in enumerate(reversed(rows)):     # age 0 = most recent
        preset = row.get("preset") or DEFAULT_PRESET
        regime = row.get("regime") or "UNKNOWN"
        if preset not in PRESETS:
            continue
        w = 0.5 ** (age / RECENCY_HALF_LIFE)
        ent = by.setdefault(regime, {}).setdefault(
            preset, {"n": 0, "w_sum": 0.0, "wx_sum": 0.0})
        ent["n"] += 1
        ent["w_sum"] += w
        ent["wx_sum"] += w * float(row.get("net_pips") or 0.0)
    out: dict = {}
    for regime, table in by.items():
        scored = {}
        for preset, e in table.items():
            mean = e["wx_sum"] / e["w_sum"] if e["w_sum"] > 0 else 0.0
            ucb = mean + UCB_C * math.sqrt(
                2 * math.log(n_total) / max(1, e["n"]))
            scored[preset] = {"n": e["n"], "weighted_mean": round(mean, 3),
                              "ucb": round(ucb, 3)}
        eligible = {p: s for p, s in scored.items() if s["n"] >= MIN_SAMPLES}
        best = (max(eligible, key=lambda p: eligible[p]["ucb"])
                if eligible else DEFAULT_PRESET)
        out[regime] = {"preset": best, "table": scored}
    return out


def get_cached(symbol: str, regime: str) -> dict:
    """Fast-path read: selected preset name + params for the live regime."""
    ent = _cache.get(symbol)
    if ent is None or time.time() - ent["ts"] > STALE_SEC:
        return {"preset": DEFAULT_PRESET, "params": PRESETS[DEFAULT_PRESET],
                "source": "default"}
    sel = (ent["by_regime"].get(regime) or {}).get("preset", DEFAULT_PRESET)
    return {"preset": sel, "params": PRESETS.get(sel, {}),
            "source": "performance"}


def maybe_refresh(db, symbol: str, model_key: str) -> None:
    """Kick an async refresh if due. Never blocks the tick path."""
    ent = _cache.get(symbol)
    if ent is not None and time.time() - ent["ts"] < REFRESH_SEC:
        return
    if symbol in _inflight:
        return
    _inflight.add(symbol)

    async def _job():
        try:
            rows = []
            async for d in db.scalp_decisions.find(
                    {"symbol": symbol, "model_key": model_key,
                     "outcome.resolved": True},
                    {"setup_preset": 1, "decision_meta.regime": 1,
                     "outcome.net_pips": 1, "ts_ms": 1}
            ).sort("ts_ms", -1).limit(500):
                rows.append({
                    "preset": d.get("setup_preset"),
                    "regime": (d.get("decision_meta") or {}).get("regime"),
                    "net_pips": (d.get("outcome") or {}).get("net_pips")})
            rows.reverse()                          # chronological
            _cache[symbol] = {"ts": time.time(),
                              "by_regime": score_presets(rows),
                              "n_rows": len(rows)}
        except Exception as e:  # noqa: BLE001
            logger.warning("strategy_select refresh failed %s: %s", symbol, e)
        finally:
            _inflight.discard(symbol)

    try:
        asyncio.get_running_loop().create_task(_job())
    except RuntimeError:
        _inflight.discard(symbol)
