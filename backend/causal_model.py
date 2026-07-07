"""iter-109 · Causal AI — structural causal graph for gold.

Instead of correlation ("gold fell after CPI") the model walks the causal
transmission chain with signs learned from live FRED data:

    inflation expectations ↑ (T10YIE)
        → Fed stays hawkish (fed_tone)
        → nominal yields ↑ (DGS10)
        → REAL yields ↑ (DGS10 − T10YIE)   ── opportunity cost of gold ↑
        → USD ↑ (DTWEXBGS)                 ── gold priced in USD
        → gold pressured ↓

Each link is checked against the last week's actual deltas; the final
`pressure` (-1 bearish … +1 bullish for gold) feeds the consensus macro
vote and renders as an explainable chain in the UI."""
import math

# Typical WEEKLY sigma per driver — used to normalise deltas to ±1.
SIGMA = {"real_yield_pp": 0.12, "usd_index": 0.60, "fed_tone": 0.5}
W_REAL, W_USD, W_FED = 0.50, 0.35, 0.15
PRESSURE_ACTIVE = 0.35


def _series_map(macro: dict) -> dict:
    return {s.get("series_id"): s for s in (macro or {}).get("series") or []}


def _norm(x: float, sigma: float) -> float:
    return math.tanh(x / sigma) if sigma > 0 else 0.0


def build_causal_view(macro: dict | None, fed_tone: dict | None = None) -> dict | None:
    sm = _series_map(macro)
    y10 = sm.get("DGS10")
    infl = sm.get("T10YIE")
    usd = sm.get("DTWEXBGS")
    if not y10 or not infl:
        return None
    d_y10 = float(y10.get("wow_delta") or 0)
    d_infl = float(infl.get("wow_delta") or 0)
    d_real = d_y10 - d_infl
    d_usd = float((usd or {}).get("wow_delta") or 0)
    tone = float((fed_tone or {}).get("score") or 0)

    n_real = _norm(d_real, SIGMA["real_yield_pp"])
    n_usd = _norm(d_usd, SIGMA["usd_index"])
    n_fed = _norm(tone, SIGMA["fed_tone"])
    pressure = round(max(-1.0, min(1.0,
        -W_REAL * n_real - W_USD * n_usd - W_FED * n_fed)), 2)

    chain = [
        {"link": "inflation expectations → yields",
         "value": f"breakeven {d_infl:+.2f}pp, 10Y {d_y10:+.2f}pp (1w)",
         "active": abs(d_infl) >= 0.03 or abs(d_y10) >= 0.05,
         "confirms": (d_infl > 0) == (d_y10 > 0) if d_infl and d_y10 else None},
        {"link": "Fed tone → yields",
         "value": f"tone {tone:+.2f} ({(fed_tone or {}).get('label', 'n/a')})",
         "active": abs(tone) >= 0.3,
         "confirms": (tone > 0) == (d_y10 > 0) if tone and d_y10 else None},
        {"link": "yields − inflation → REAL yields",
         "value": f"real yield {d_real:+.2f}pp (1w)",
         "active": abs(d_real) >= 0.04, "confirms": None},
        {"link": "real yields → USD",
         "value": f"USD index {d_usd:+.2f} (1w)",
         "active": abs(d_usd) >= 0.2,
         "confirms": (d_real > 0) == (d_usd > 0) if d_real and d_usd else None},
        {"link": "real yields + USD → gold",
         "value": f"pressure {pressure:+.2f}",
         "active": abs(pressure) >= PRESSURE_ACTIVE, "confirms": None},
    ]
    if pressure <= -PRESSURE_ACTIVE:
        narrative = (f"{'Rising' if d_infl > 0 else 'Sticky'} inflation → "
                     f"{'higher' if d_y10 > 0 else 'firm'} yields → real yields "
                     f"{d_real:+.2f}pp → USD {d_usd:+.2f} → gold PRESSURED "
                     f"({pressure:+.2f})")
        label = "BEARISH_GOLD"
    elif pressure >= PRESSURE_ACTIVE:
        narrative = (f"{'Falling' if d_y10 < 0 else 'Soft'} yields → real yields "
                     f"{d_real:+.2f}pp → USD {d_usd:+.2f} → gold SUPPORTED "
                     f"({pressure:+.2f})")
        label = "BULLISH_GOLD"
    else:
        narrative = (f"Transmission chain quiet: real yields {d_real:+.2f}pp, "
                     f"USD {d_usd:+.2f} — no dominant causal pressure "
                     f"({pressure:+.2f})")
        label = "NEUTRAL"
    return {"pressure": pressure, "label": label, "narrative": narrative,
            "chain": chain,
            "inputs": {"d_y10_1w": d_y10, "d_infl_1w": d_infl,
                       "d_real_1w": round(d_real, 3), "d_usd_1w": d_usd,
                       "fed_tone": tone}}
