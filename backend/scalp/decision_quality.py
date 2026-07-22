"""Phase C · Combined decision verdict — ONE score from the five pillars:

    Prediction Quality + Execution Quality + Expected Value
                       + Market Regime + Risk Context

Each pillar is normalised to 0..1, weighted, and combined into a 0-100
score with an explainable per-pillar breakdown. Deterministic and pure.

    STRONG ≥ 70 · OK ≥ 55 · WEAK ≥ 45 · BLOCKED < 45 (no live submission)
"""

WEIGHTS = {"prediction": 0.25, "execution": 0.20, "ev": 0.25,
           "regime": 0.20, "risk": 0.10}
MIN_LIVE_SCORE = 45.0
STRONG = 70.0
OK = 55.0


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def combine(*, p_win: float, model_source: str = "model",
            ev_pips: float, cost_pips: float,
            exec_score: float,
            regime: str = "UNKNOWN", regime_confidence: float = 0.0,
            direction_aligned: bool = True, h1_agrees: bool = False,
            risk_headroom_frac: float = 1.0,
            loss_streak: int = 0) -> dict:
    pillars: dict = {}

    # 1 · prediction quality: calibrated edge above coin-flip; heuristic
    # fallbacks (no trained model) are discounted
    pillars["prediction"] = round(
        _clamp((float(p_win) - 0.45) / 0.25)
        * (1.0 if model_source == "model" else 0.7), 3)

    # 2 · execution quality (0-100 from exec_quality.py)
    pillars["execution"] = round(_clamp(float(exec_score) / 100.0), 3)

    # 3 · expected value: EV must clear costs with margin; EV ≥ 2× costs
    # earns full marks, non-positive EV earns zero
    if ev_pips is None or ev_pips <= 0:
        pillars["ev"] = 0.0
    else:
        pillars["ev"] = round(_clamp(float(ev_pips)
                                     / (2.0 * max(float(cost_pips), 0.1))), 3)

    # 4 · market regime: alignment × confidence, H1 agreement tilts
    if not direction_aligned:
        pillars["regime"] = 0.0
    elif regime in ("UNKNOWN", None):
        pillars["regime"] = 0.3
    else:
        pillars["regime"] = round(
            _clamp(float(regime_confidence)
                   * (1.1 if h1_agrees else 0.9)), 3)

    # 5 · risk context: remaining daily-loss headroom, streak-dampened
    pillars["risk"] = round(_clamp(float(risk_headroom_frac))
                            * (0.5 if loss_streak >= 2 else 1.0), 3)

    score = round(100.0 * sum(WEIGHTS[k] * pillars[k] for k in WEIGHTS), 1)
    if score >= STRONG:
        verdict = "STRONG"
    elif score >= OK:
        verdict = "OK"
    elif score >= MIN_LIVE_SCORE:
        verdict = "WEAK"
    else:
        verdict = "BLOCKED"
    weakest = sorted(pillars, key=lambda k: pillars[k])[:2]
    return {"score": score, "verdict": verdict, "pillars": pillars,
            "weights": WEIGHTS, "weakest": weakest,
            "live_allowed": verdict != "BLOCKED"}
