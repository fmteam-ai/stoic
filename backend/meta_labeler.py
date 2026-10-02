"""Meta-Labeler — third-tier verification on top of Quant + Semantic engines.

Architecture (per López de Prado, 'Advances in Financial ML'):

    Engine 1: Quant Module     → indicators + regime + entropy   (deterministic)
    Engine 2: Semantic LLM     → Claude action + news sentiment  (probabilistic)
    Engine 3: Meta-Labeler     → P(true | engine_outputs)        (this module)

The Meta-Labeler never decides BUY/SELL. It only answers:
    "Given what Engines 1 and 2 said, is this a TRUE signal or a FAKE-OUT?"

Implementation: a hand-engineered logistic classifier trained on the disagreement
structure between the two engines + market context. Weights are chosen from
the López de Prado / Hudson & Thames defaults; can be re-fit later from
historical trade outcomes stored in MongoDB once enough samples accumulate.

Threshold defaults to 0.55 — anything below blocks execution even if confidence
is high, because high confidence + structural disagreement = classic fake-out.

DEPRECATED (uncertainty upgrade): the hand-weighted logistic below was never
fitted to outcomes and is not called by the trading loop. The learned
replacement is ``meta_labeling`` (triple-barrier labels over executed AND
vetoed signals, uniqueness weights, purged walk-forward, Platt/isotonic
calibration). ``predict_true_signal_probability`` is kept for importers
(tests/backend_test.py); new code should call
``predict_true_signal_probability_learned`` (async), which uses the learned
meta-model when one is trained for the user and falls back to the legacy
hand weights otherwise.
"""
from __future__ import annotations
import math
from typing import Dict


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# Hand-crafted feature weights — bias toward agreement across engines.
# Positive = pushes toward "true signal" (allow execution).
# Negative = pushes toward "fake-out" (block execution).
WEIGHTS = {
    "bias": -0.4,
    "confidence_norm": 2.5,         # raw model conviction
    "sentiment_alignment": 1.3,     # +1 if news agrees with chart, -1 if opposes
    "regime_quality": 1.4,          # clean trend regimes get a boost
    "entropy_penalty": -1.8,        # high entropy = noisy = penalize
    "session_quality": 0.6,         # London/NY overlap = best fill quality
    "rsi_extreme_penalty": -0.7,    # entries at RSI 30/70 are risky
    "macro_proximity_penalty": -1.2,  # nearby high-impact news = blockable
    "vol_extreme_penalty": -0.5,    # outlier vol pollutes the signal
}


def _regime_quality(regime: dict) -> float:
    label = (regime or {}).get("regime", "TRANSITIONAL")
    return {
        "LOW_VOL_TREND": 1.0,
        "HIGH_VOL_TREND": 0.6,
        "RANGE": 0.2,
        "TRANSITIONAL": -0.3,
        "CHOP": -1.0,
    }.get(label, 0.0)


def _sentiment_alignment(action: str, sentiment: dict) -> float:
    """+1 if news agrees with chart action, -1 if opposes, 0 if neutral."""
    score = float((sentiment or {}).get("score") or 0)
    if abs(score) < 0.15:
        return 0.0
    if action == "BUY":
        return 1.0 if score > 0 else -1.0
    if action == "SELL":
        return 1.0 if score < 0 else -1.0
    return 0.0


def _rsi_extreme(indicators: dict, action: str) -> float:
    """Penalise entries near oversold/overbought (often false breakouts)."""
    rsi = (indicators or {}).get("rsi_14")
    if rsi is None:
        return 0.0
    if action == "BUY" and rsi > 70:
        return min((rsi - 70) / 30.0, 1.0)
    if action == "SELL" and rsi < 30:
        return min((30 - rsi) / 30.0, 1.0)
    return 0.0


def _macro_proximity(upcoming_macro: list) -> float:
    """0 → 1 penalty for nearby high-impact news in the next 24h."""
    if not upcoming_macro:
        return 0.0
    weight = 0.0
    for ev in upcoming_macro[:3]:
        impact = (ev.get("impact") or "").lower()
        if impact == "high":
            weight += 0.4
        elif impact == "medium":
            weight += 0.15
    return min(weight, 1.0)


def _vol_extreme(indicators: dict) -> float:
    vol = (indicators or {}).get("volatility_30d_pct") or 0
    if vol > 5:   # outlier vol
        return min((vol - 5) / 5.0, 1.0)
    if vol < 0.3:  # dead market — likely fake breakout
        return 0.6
    return 0.0


def _session_quality(session: dict) -> float:
    if not session:
        return 0.0
    if session.get("is_high_volume_window"):
        return 1.0
    if session.get("primary") in ("london", "ny"):
        return 0.6
    if session.get("is_weekend"):
        return -0.3
    return 0.3


def predict_true_signal_probability(
    *,
    action: str,
    confidence: float,
    sentiment: dict,
    regime: dict,
    entropy: dict,
    session: dict,
    indicators: dict,
    upcoming_macro: list,
) -> Dict:
    """Return Meta-Labeler probability that this signal is a TRUE trend (not fake-out).

    Output shape:
        {
          "p_true": 0..1,
          "verdict": "TRUE_SIGNAL" | "FAKE_OUT",
          "threshold": 0.55,
          "features": {...explainability dict...}
        }
    """
    if action == "HOLD":
        return {
            "p_true": 0.0,
            "verdict": "NEUTRAL",
            "threshold": 0.55,
            "features": {"reason": "no action to verify"},
        }

    features = {
        "confidence_norm": (confidence - 50.0) / 50.0,        # ∈ [-1, 1]
        "sentiment_alignment": _sentiment_alignment(action, sentiment),
        "regime_quality": _regime_quality(regime),
        "entropy_penalty": float(entropy.get("entropy") or 0),  # ∈ [0,1]
        "session_quality": _session_quality(session),
        "rsi_extreme_penalty": _rsi_extreme(indicators, action),
        "macro_proximity_penalty": _macro_proximity(upcoming_macro),
        "vol_extreme_penalty": _vol_extreme(indicators),
    }

    z = WEIGHTS["bias"]
    for k, v in features.items():
        z += WEIGHTS[k] * v

    p = _sigmoid(z)
    threshold = 0.55
    verdict = "TRUE_SIGNAL" if p >= threshold else "FAKE_OUT"

    return {
        "p_true": round(p, 4),
        "verdict": verdict,
        "threshold": threshold,
        "logit": round(z, 4),
        "features": {k: round(v, 4) for k, v in features.items()},
        "source": "hand_weighted_deprecated",
    }


async def predict_true_signal_probability_learned(
        db, user_id: str, signal: dict, bars: list,
        account_id: str | None = None, threshold: float = 0.55,
        **legacy_kwargs) -> Dict:
    """Learned meta-label P(true signal) via meta_labeling.predict; falls
    back to the deprecated hand-weighted model (needs the legacy keyword
    arguments) when no meta-model is trained. Never raises."""
    action = (signal or {}).get("action")
    if action in ("BUY", "SELL"):
        try:
            import meta_labeling
            res = await meta_labeling.predict(db, user_id, signal, bars or [],
                                              account_id=account_id)
        except Exception:  # noqa: BLE001 — fail-open
            res = None
        if res:
            p = float(res["p_success"])
            return {"p_true": round(p, 4),
                    "verdict": "TRUE_SIGNAL" if p >= threshold else "FAKE_OUT",
                    "threshold": threshold, "source": "meta_labeling",
                    "calibration": res.get("calibration"),
                    "n_train": res.get("n_train")}
    if legacy_kwargs:
        kw = {"action": action or "HOLD",
              "confidence": float((signal or {}).get("confidence") or 50.0),
              "sentiment": {}, "regime": {}, "entropy": {}, "session": {},
              "indicators": {}, "upcoming_macro": []}
        kw.update(legacy_kwargs)
        return predict_true_signal_probability(**kw)
    return {"p_true": None, "verdict": "UNKNOWN", "threshold": threshold,
            "source": "none"}
