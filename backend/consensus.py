"""iter-63 · Master Agent — weighted consensus across all agents.

Combines every agent's view of a candidate trade into one 0-100 score:
  trend (MTF tiers)      20%
  quant (conf + RL)      20%
  structure (SMC)        15%
  forecast (Chronos)     20%
  liquidity (order flow) 15%
  macro (Fed + tape)     10%
50 = neutral. The Risk agent is intentionally NOT in the score — its rules
(loss cooldown, payoff guard, EOD quiet, …) stay hard gates.
A trade only fires when score ≥ `consensus_threshold` (default 55) unless
`consensus_gate_mode` is advisory/off."""

WEIGHTS = {"trend": 0.20, "quant": 0.20, "structure": 0.15,
           "forecast": 0.20, "liquidity": 0.15, "macro": 0.10}
DEFAULT_THRESHOLD = 55


def _dir_vote(action: str, direction) -> float:
    if direction == "UP":
        return 1.0 if action == "BUY" else -1.0
    if direction == "DOWN":
        return 1.0 if action == "SELL" else -1.0
    return 0.0


def _clip(v):
    return max(-1.0, min(1.0, v))


def compute_consensus(signal: dict) -> dict:
    action = signal.get("action")
    votes = {}

    tiers = signal.get("mtf_tiers") or {}
    tv = sum(w * _dir_vote(action, ((tiers.get(k) or {}).get("direction")))
             for k, w in (("SHORT", 0.5), ("MEDIUM", 0.3), ("LONG", 0.2)))
    votes["trend"] = round(_clip(tv), 2)

    st = signal.get("market_structure") or {}
    sv = 0.0
    if st.get("ready"):
        bias = st.get("bias")
        if bias == "BULLISH":
            sv = 1.0 if action == "BUY" else -1.0
        elif bias == "BEARISH":
            sv = 1.0 if action == "SELL" else -1.0
        phase = (st.get("acc_dist") or {}).get("phase")
        if phase == "ACCUMULATION":
            sv += 0.3 if action == "BUY" else -0.3
        elif phase == "DISTRIBUTION":
            sv += 0.3 if action == "SELL" else -0.3
    votes["structure"] = round(_clip(sv), 2)

    fc = signal.get("forecast") or {}
    fv = 0.0
    if fc.get("q50") is not None:
        last = fc["last"]
        band_up = fc.get("q10", last) > last       # entire band above
        band_dn = fc.get("q90", last) < last       # entire band below
        med_up = fc["q50"] > last
        if action == "BUY":
            fv = 1.0 if band_up else -1.0 if band_dn else (0.5 if med_up else -0.5)
        elif action == "SELL":
            fv = 1.0 if band_dn else -1.0 if band_up else (-0.5 if med_up else 0.5)
    votes["forecast"] = round(fv, 2)

    qv = _clip((float(signal.get("confidence") or 50) - 60) / 20)
    rl = signal.get("rl_policy") or {}
    if rl.get("decision") == "BLOCK":
        qv -= 0.8
    elif rl.get("decision") == "SCALE":
        qv -= 0.4
    elif (rl.get("mean") or 0) > 0 and rl.get("n", 0) >= 8:
        qv += 0.4
    bayes = signal.get("bayes") or {}
    if bayes.get("n", 0) >= 8 and bayes.get("ev_r") is not None:
        qv += _clip(bayes["ev_r"]) * 0.4
    votes["quant"] = round(_clip(qv), 2)

    lm = signal.get("liquidity") or {}
    lv = 0.0
    if lm.get("ready"):
        draw = lm.get("draw")
        if draw == "UP":
            lv += 0.6 if action == "BUY" else -0.6
        elif draw == "DOWN":
            lv += 0.6 if action == "SELL" else -0.6
        zone = lm.get("active_zone")
        if zone == "DEMAND":
            lv += 0.4 if action == "BUY" else -0.4
        elif zone == "SUPPLY":
            lv += 0.4 if action == "SELL" else -0.4
        delta = (lm.get("cum_delta") or {}).get("bias")
        if delta == "BULLISH":
            lv += 0.3 if action == "BUY" else -0.3
        elif delta == "BEARISH":
            lv += 0.3 if action == "SELL" else -0.3
        dom = lm.get("dom") or {}
        imb = dom.get("imbalance")
        if dom.get("live") and imb is not None and abs(imb) >= 0.25:
            lv += 0.3 if (imb > 0) == (action == "BUY") else -0.3
    votes["liquidity"] = round(_clip(lv), 2)

    mv = 0.0
    tone_score = float((signal.get("fed_tone") or {}).get("score") or 0)
    if abs(tone_score) >= 0.3:      # hawkish Fed = strong USD = gold-bearish
        mv += -tone_score if action == "BUY" else tone_score
    ch = (signal.get("intraday_momentum") or {}).get("change_pct")
    if ch is not None and abs(ch) >= 0.2:
        mv += 0.5 if (ch > 0) == (action == "BUY") else -0.5
    news = signal.get("news_ai") or {}
    net = news.get("net")
    if net is not None and abs(float(net)) >= 0.75:   # AI news read (-3..+3)
        mv += 0.5 if (float(net) > 0) == (action == "BUY") else -0.5
    votes["macro"] = round(_clip(mv), 2)

    total = sum(WEIGHTS[k] * votes[k] for k in WEIGHTS)
    score = max(0, min(100, round(50 + 50 * total)))
    verdict = ("STRONG" if score >= 70 else "OK" if score >= DEFAULT_THRESHOLD
               else "WEAK" if score >= 40 else "CONFLICTED")
    return {"score": score, "votes": votes, "verdict": verdict}
