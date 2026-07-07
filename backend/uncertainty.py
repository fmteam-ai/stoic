"""iter-110 · Uncertainty Estimation — every prediction gets a calibrated
confidence and a risk tier.

Raw model outputs are overconfident. This layer measures HOW SURE the whole
stack really is by combining:
  · model disagreement    — spread of the 7 ensemble members' P(win)
  · evidence thinness     — Bayes 90% credible-interval width & sample count
  · forecast dispersion   — Chronos band width vs its median move
  · agent conflict        — consensus votes pointing against the action

Calibrated confidence shrinks the directional estimate toward 50% by total
uncertainty:  p_cal = 0.5 + (p_dir − 0.5)·(1 − U).
Risk tiers:  LOW (conf ≥75, U ≤.35) · MEDIUM · HIGH (conf <60 or U ≥.6).
`uncertainty_gate` skips HIGH-risk / low-confidence trades (default enforce)."""
import math

W = {"model_disagreement": 0.30, "bayes_ci": 0.20, "forecast_band": 0.15,
     "agent_conflict": 0.25, "data_sufficiency": 0.10}
RISK_LOW_CONF = 75
RISK_HIGH_CONF = 60
RISK_LOW_U = 0.35
RISK_HIGH_U = 0.60


def estimate_uncertainty(signal: dict) -> dict:
    comps, drivers = {}, []

    ml = signal.get("ml_ensemble") or {}
    members = ml.get("members") or []
    if len(members) >= 3:
        ps = [float(m["p"]) for m in members]
        mean = sum(ps) / len(ps)
        spread = math.sqrt(sum((p - mean) ** 2 for p in ps) / len(ps))
        comps["model_disagreement"] = min(1.0, spread / 0.25)
        if spread > 0.15:
            drivers.append(f"the {len(members)} models disagree "
                           f"(spread ±{round(spread * 100)}pp)")

    b = signal.get("bayes") or {}
    ci = b.get("ci90")
    if ci and len(ci) == 2:
        width = float(ci[1]) - float(ci[0])
        comps["bayes_ci"] = min(1.0, max(0.0, (width - 0.2) / 0.5))
        if width > 0.4:
            drivers.append(f"thin evidence: 90% CI spans "
                           f"{round(width * 100)}pp (n={b.get('n', 0)})")

    fc = signal.get("forecast") or {}
    if fc.get("band_high_pct") is not None and fc.get("band_low_pct") is not None:
        bw = abs(float(fc["band_high_pct"]) - float(fc["band_low_pct"]))
        med = abs(float(fc.get("median_change_pct") or 0))
        ratio = bw / max(med, 0.05)
        comps["forecast_band"] = min(1.0, ratio / 20.0)
        if ratio > 12:
            drivers.append("forecast band much wider than its median move")

    cons = signal.get("consensus") or {}
    votes = cons.get("votes") or {}
    if votes:
        opposing = sum(1 for v in votes.values() if float(v) < -0.05)
        comps["agent_conflict"] = min(1.0, 1.5 * opposing / len(votes))
        if opposing >= 2:
            names = [k for k, v in votes.items() if float(v) < -0.05]
            drivers.append(f"{opposing} agents vote against "
                           f"({', '.join(names)})")

    n_evi = max(int(b.get("n") or 0),
                int((signal.get("rl_policy") or {}).get("n") or 0))
    comps["data_sufficiency"] = max(0.0, 1.0 - min(n_evi / 20.0, 1.0))
    if n_evi < 8:
        drivers.append(f"only {n_evi} similar past trades to learn from")

    used = {k: v for k, v in comps.items() if k in W}
    tw = sum(W[k] for k in used)
    u = sum(W[k] * used[k] for k in used) / tw if tw > 0 else 0.5

    p_parts = []
    if cons.get("score") is not None:
        p_parts.append(float(cons["score"]) / 100.0)
    if ml.get("p_win") is not None:
        p_parts.append(float(ml["p_win"]))
    if signal.get("confidence") is not None:
        p_parts.append(float(signal["confidence"]) / 100.0)
    p_dir = sum(p_parts) / len(p_parts) if p_parts else 0.5
    p_cal = 0.5 + (p_dir - 0.5) * (1.0 - u)
    conf = round(100 * p_cal)
    risk = ("LOW" if conf >= RISK_LOW_CONF and u <= RISK_LOW_U else
            "HIGH" if conf < RISK_HIGH_CONF or u >= RISK_HIGH_U else
            "MEDIUM")
    return {"confidence_pct": conf, "risk": risk, "uncertainty": round(u, 2),
            "raw_direction_pct": round(100 * p_dir),
            "components": {k: round(v, 2) for k, v in comps.items()},
            "drivers": drivers[:4]}


def uncertainty_gate(est: dict | None, min_conf: int = 60) -> str | None:
    """Skip low-confidence trades. Fires on HIGH risk or conf < threshold."""
    if not est:
        return None
    why = "; ".join(est.get("drivers") or []) or "combined model uncertainty"
    if est["risk"] == "HIGH":
        return (f"Uncertainty gate: calibrated confidence "
                f"{est['confidence_pct']}% · risk HIGH "
                f"(uncertainty {est['uncertainty']}) — trade skipped. {why}.")
    if est["confidence_pct"] < min_conf:
        return (f"Uncertainty gate: calibrated confidence "
                f"{est['confidence_pct']}% below the {min_conf}% floor — "
                f"trade skipped. {why}.")
    return None
