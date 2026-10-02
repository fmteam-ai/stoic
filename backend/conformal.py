"""Conformal prediction for the quantile forecaster (uncertainty upgrade).

The Chronos deciles (q10..q90) are a *model's* belief about its 80% band —
nothing guarantees that 80% of realised prices actually land inside it.
This module turns them into intervals with an empirical coverage guarantee:

* **Split conformal / CQR** (Romano, Patterson & Candès 2019):
  nonconformity score  s_i = max(q_lo_i − y_i, y_i − q_hi_i)  on a
  calibration set; the conformalised band is
  [q_lo − Q̂, q_hi + Q̂] with Q̂ the ⌈(n+1)(1−α)⌉/n empirical quantile of s.
  Q̂ < 0 NARROWS an over-wide band, Q̂ > 0 widens an over-confident one.

* **Adaptive Conformal Inference** (Gibbs & Candès 2021): the data are a
  non-exchangeable time series, so the working miscoverage level α_t is
  updated online,  α_{t+1} = α_t + γ (α − err_t),  err_t = 1{y_t ∉ band_t}.
  Long-run realised coverage converges to 1−α for ANY data sequence.

Scores are NORMALISED by the forecaster's own band width (q_hi − q_lo) —
the "scaled CQR" variant — so one state works across price levels AND the
margin follows volatility regimes as fast as the forecaster's band does. State is tiny and JSON-serialisable; the async
helpers persist it in ``db.forecast_conformal_state`` — one document per
user+symbol — and the forecaster stays fail-open: no state / too few
resolved forecasts → the raw deciles are used unchanged (pre-upgrade
behaviour).

Config (env):
    FORECAST_CONFORMAL_ENABLED   default "true"
    CONFORMAL_TARGET_COVERAGE    default 0.80 (the q10..q90 band)
    CONFORMAL_GAMMA              default 0.01 (ACI step size)
    CONFORMAL_WINDOW             default 250 (rolling score window)
    CONFORMAL_MIN_UPDATES        default 20 (resolved forecasts before the
                                  conformal band replaces the raw one)
"""
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger("conformal")

COLLECTION = "forecast_conformal_state"
MAX_PENDING = 48


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def enabled() -> bool:
    return os.environ.get("FORECAST_CONFORMAL_ENABLED",
                          "true").lower() == "true"


def target_alpha() -> float:
    cov = _env_float("CONFORMAL_TARGET_COVERAGE", 0.80)
    return float(min(max(1.0 - cov, 1e-3), 0.999))


def gamma() -> float:
    return _env_float("CONFORMAL_GAMMA", 0.01)


def window() -> int:
    return int(_env_float("CONFORMAL_WINDOW", 250))


def min_updates() -> int:
    return int(_env_float("CONFORMAL_MIN_UPDATES", 20))


def _width(q_lo: float, q_hi: float) -> float:
    w = float(q_hi) - float(q_lo)
    return w if w > 0 else max(abs(float(q_hi)) * 1e-6, 1e-12)


# --------------------------------------------------------------- split CQR
def cqr_scores(q_lo, q_hi, y) -> np.ndarray:
    """CQR nonconformity: positive when y falls outside [q_lo, q_hi]."""
    q_lo, q_hi, y = (np.asarray(a, dtype=float) for a in (q_lo, q_hi, y))
    return np.maximum(q_lo - y, y - q_hi)


def conformal_quantile(scores, alpha: float) -> float:
    """Finite-sample-corrected (1−α) empirical quantile of the scores.
    α ≤ 0 → +inf (trivial band); α ≥ 1 → −inf (empty band)."""
    s = np.sort(np.asarray(scores, dtype=float))
    n = len(s)
    if n == 0:
        return 0.0
    if alpha <= 0:
        return float("inf")
    if alpha >= 1:
        return float("-inf")
    k = math.ceil((n + 1) * (1.0 - alpha))
    if k > n:
        return float("inf")
    return float(s[max(k, 1) - 1])


def split_conformal_margin(q_lo, q_hi, y, alpha: float) -> float:
    """Split-conformal additive margin Q̂ for a target miscoverage α."""
    return conformal_quantile(cqr_scores(q_lo, q_hi, y), alpha)


# ------------------------------------------------------------------- ACI
@dataclass
class ACIState:
    """Online Adaptive Conformal Inference over CQR scores (return units)."""
    alpha: float = 0.2              # target miscoverage
    gamma: float = 0.01             # step size
    alpha_t: float = 0.2            # working level
    window: int = 250
    scores: list = field(default_factory=list)
    raw_hits: list = field(default_factory=list)    # 1 = covered by raw band
    conf_hits: list = field(default_factory=list)   # 1 = covered by conformal band
    n_updates: int = 0
    n_cov_raw: int = 0              # cumulative (long-run) coverage counts
    n_cov_conf: int = 0

    # -- band
    def margin(self) -> float:
        """Additive margin (return units) at the current working level."""
        if not self.scores:
            return 0.0
        s = np.asarray(self.scores[-self.window:], dtype=float)
        if self.alpha_t <= 0:
            # trivial-band regime: widen to the worst score seen (finite)
            return float(np.max(s))
        if self.alpha_t >= 1:
            return float(np.min(s))
        return float(np.quantile(s, 1.0 - self.alpha_t))

    def interval(self, q_lo: float, q_hi: float, scale: float | None = None):
        scale = _width(q_lo, q_hi) if scale is None else scale
        m = self.margin() * scale
        lo, hi = q_lo - m, q_hi + m
        if lo > hi:                       # never invert the band
            mid = 0.5 * (q_lo + q_hi)
            lo = hi = mid
        return lo, hi

    # -- online update
    def update(self, q_lo: float, q_hi: float, y: float,
               scale: float | None = None,
               margin_used: float | None = None) -> bool:
        """Feed one realised outcome. `scale` normalises the score (default
        the band width q_hi − q_lo); `margin_used` is the (normalised)
        margin that was actually applied when the forecast was issued
        (defaults to the current margin). Returns True when the conformal
        band covered y."""
        scale = _width(q_lo, q_hi) if scale is None else scale
        scale = scale if scale and scale > 0 else 1.0
        m = self.margin() if margin_used is None else float(margin_used)
        lo, hi = q_lo - m * scale, q_hi + m * scale
        covered = bool(lo <= y <= hi)
        err = 0.0 if covered else 1.0
        self.alpha_t = float(self.alpha_t + self.gamma * (self.alpha - err))
        # keep α_t in a sane range (Gibbs & Candès allow it to leave [0,1];
        # bounded drift avoids an unrecoverable state after a long shock)
        self.alpha_t = float(min(max(self.alpha_t, -0.5), 1.5))
        score = float(max(q_lo - y, y - q_hi)) / scale
        self.scores.append(score)
        self.raw_hits.append(1 if q_lo <= y <= q_hi else 0)
        self.conf_hits.append(1 if covered else 0)
        for lst in (self.scores, self.raw_hits, self.conf_hits):
            if len(lst) > self.window:
                del lst[:len(lst) - self.window]
        self.n_updates += 1
        self.n_cov_raw += 1 if q_lo <= y <= q_hi else 0
        self.n_cov_conf += 1 if covered else 0
        return covered

    # -- diagnostics / persistence
    def coverage(self) -> dict:
        def _m(v):
            return round(float(np.mean(v)), 3) if v else None
        n = max(self.n_updates, 1)
        return {"raw": _m(self.raw_hits), "conformal": _m(self.conf_hits),
                "target": round(1.0 - self.alpha, 3), "n": len(self.raw_hits),
                "long_run_raw": round(self.n_cov_raw / n, 3)
                if self.n_updates else None,
                "long_run_conformal": round(self.n_cov_conf / n, 3)
                if self.n_updates else None}

    def to_doc(self) -> dict:
        return {"alpha": self.alpha, "gamma": self.gamma,
                "alpha_t": self.alpha_t, "window": self.window,
                "scores": [round(s, 8) for s in self.scores],
                "raw_hits": list(self.raw_hits),
                "conf_hits": list(self.conf_hits),
                "n_updates": int(self.n_updates),
                "n_cov_raw": int(self.n_cov_raw),
                "n_cov_conf": int(self.n_cov_conf)}

    @classmethod
    def from_doc(cls, doc: dict | None, **defaults) -> "ACIState":
        d = dict(defaults)
        for k in ("alpha", "gamma", "alpha_t", "window", "scores",
                  "raw_hits", "conf_hits", "n_updates", "n_cov_raw",
                  "n_cov_conf"):
            if doc and doc.get(k) is not None:
                d[k] = doc[k]
        st = cls(**d)
        st.scores = [float(s) for s in st.scores]
        return st


def new_state() -> ACIState:
    a = target_alpha()
    return ACIState(alpha=a, gamma=gamma(), alpha_t=a, window=window())


# ------------------------------------------------- decile conformalisation
def conformalize_quantiles(levels, values, margin_price: float) -> list:
    """Shift a decile vector by the conformal margin: the outer quantiles
    move by ±margin, the median not at all, linear in between; the result
    is re-sorted to stay a valid (monotone) quantile function."""
    levels = [float(x) for x in levels]
    values = [float(v) for v in values]
    if not values:
        return values
    lo_l, hi_l = levels[0], levels[-1]
    out = []
    for lv, v in zip(levels, values):
        if lv < 0.5:
            w = (0.5 - lv) / max(0.5 - lo_l, 1e-9)
            out.append(v - w * margin_price)
        elif lv > 0.5:
            w = (lv - 0.5) / max(hi_l - 0.5, 1e-9)
            out.append(v + w * margin_price)
        else:
            out.append(v)
    return [float(x) for x in np.maximum.accumulate(np.asarray(out))]


def conformal_block(fc: dict, st: ACIState | None) -> dict | None:
    """Conformalised view of a forecast payload, or None when the state is
    absent / immature (callers then use the raw deciles, as before)."""
    if st is None or st.n_updates < min_updates() or not fc:
        return None
    last = float(fc["last"])
    m_rel = st.margin()
    if not math.isfinite(m_rel):
        return None
    m_px = m_rel * _width(float(fc["q10"]), float(fc["q90"]))
    lo, hi = st.interval(float(fc["q10"]), float(fc["q90"]))
    out = {"active": True, "method": "aci_scaled_cqr",
           "alpha_t": round(st.alpha_t, 4),
           "margin": round(m_px, 5),
           "margin_band_frac": round(m_rel, 4),
           "q10": round(lo, 5), "q90": round(hi, 5),
           "band_low_pct": round((lo - last) / last * 100, 3),
           "band_high_pct": round((hi - last) / last * 100, 3),
           "coverage": st.coverage(), "n_updates": st.n_updates}
    if fc.get("quantile_values") and fc.get("quantile_levels"):
        out["quantile_levels"] = list(fc["quantile_levels"])
        out["quantile_values"] = [round(v, 5) for v in conformalize_quantiles(
            fc["quantile_levels"], fc["quantile_values"], m_px)]
    return out


def effective_band(fc: dict | None) -> tuple | None:
    """(q10, q90) the gates should use: conformal when active, else raw."""
    if not fc:
        return None
    c = fc.get("conformal") or {}
    if c.get("active"):
        return float(c["q10"]), float(c["q90"])
    return float(fc["q10"]), float(fc["q90"])


# -------------------------------------------- pending-forecast resolution
STEP_SECONDS = {"M15": 900, "daily": 86400}


def register_pending(pending: list, fc: dict, now_ts: float,
                     margin_rel: float) -> list:
    """Append the issued forecast (once per source step) for later scoring."""
    step = STEP_SECONDS.get(fc.get("source"), 900)
    if pending and now_ts - float(pending[-1].get("made_at", 0)) < step:
        return pending
    pending = list(pending) + [{
        "made_at": float(now_ts),
        "due_ts": float(now_ts + step * int(fc.get("horizon") or 1)),
        "last": float(fc["last"]), "q10": float(fc["q10"]),
        "q90": float(fc["q90"]), "margin_rel": float(margin_rel),
        "source": fc.get("source")}]
    return pending[-MAX_PENDING:]


def _price_at(bars: list, ts: float):
    """Close of the first bar whose open time is ≥ ts (None if not yet)."""
    for b in bars or []:
        try:
            if float(b.get("t") or 0) >= ts:
                return float(b["c"])
        except (TypeError, ValueError, KeyError):
            continue
    return None


def resolve_pending(st: ACIState, pending: list, bars: list,
                    now_ts: float, max_age_s: float = 7 * 86400) -> list:
    """Score every pending forecast whose horizon has elapsed and whose
    realised price is visible in `bars`; drop stale ones. Mutates `st`."""
    keep = []
    for p in pending or []:
        due = float(p.get("due_ts") or 0)
        if now_ts < due:
            keep.append(p)
            continue
        y = _price_at(bars, due)
        if y is None:
            if now_ts - due < max_age_s:
                keep.append(p)
            continue
        st.update(p["q10"], p["q90"], y,
                  margin_used=p.get("margin_rel", p.get("margin_ret")))
    return keep


async def load_state(db, user_id: str, symbol: str):
    doc = await db[COLLECTION].find_one({"_id": f"{user_id}:{symbol}"})
    return doc


async def save_state(db, user_id: str, symbol: str, st: ACIState,
                     pending: list) -> None:
    await db[COLLECTION].update_one(
        {"_id": f"{user_id}:{symbol}"},
        {"$set": {**st.to_doc(), "user_id": user_id, "symbol": symbol,
                  "pending": pending, "updated_at": time.time()}},
        upsert=True)


async def apply_to_forecast(db, user_id: str, symbol: str, fc: dict | None,
                            bars: list | None, now_ts: float | None = None):
    """Per-user conformal step around a (possibly shared/cached) forecast:
    resolve matured forecasts, update ACI, register this one, and return a
    COPY of `fc` carrying a `conformal` block when the state is mature.
    Fail-open: any error returns `fc` unchanged."""
    if not fc or db is None or not enabled():
        return fc
    try:
        now_ts = time.time() if now_ts is None else now_ts
        doc = await load_state(db, user_id, symbol)
        st = ACIState.from_doc(doc, alpha=target_alpha(), gamma=gamma(),
                               alpha_t=target_alpha(), window=window())
        pending = resolve_pending(st, (doc or {}).get("pending") or [],
                                  bars or [], now_ts)
        m = st.margin() if st.n_updates >= min_updates() else 0.0
        if not math.isfinite(m):
            m = 0.0
        pending = register_pending(pending, fc, now_ts, m)
        await save_state(db, user_id, symbol, st, pending)
        block = conformal_block(fc, st)
        out = dict(fc)
        if block:
            out["conformal"] = block
        else:
            out["conformal"] = {"active": False, "n_updates": st.n_updates,
                                "needed": min_updates(),
                                "coverage": st.coverage()}
        return out
    except Exception as e:  # noqa: BLE001 — never break the forecaster
        logger.debug("conformal step failed: %s", e)
        return fc
