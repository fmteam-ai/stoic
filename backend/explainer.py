"""iter-113 · Explainable AI — every decision explains itself.

Builds a human-readable rationale from the agent stack already attached to
the signal: "I bought because trend tiers aligned, liquidity draw UP, real
yields falling → USD weakening, news +1.2/3 for gold, ensemble P(win) 68%,
Monte Carlo EV +0.31R — calibrated confidence 72%." Supporting agents go in
`because`, opposing ones in `despite`. Stored on the signal for debugging
and shown in the UI."""


def _tier_txt(tiers):
    ups = [k for k in ("SHORT", "MEDIUM", "LONG")
           if (tiers.get(k) or {}).get("direction") == "UP"]
    dns = [k for k in ("SHORT", "MEDIUM", "LONG")
           if (tiers.get(k) or {}).get("direction") == "DOWN"]
    return ups, dns


def explain_decision(signal: dict) -> dict:
    action = signal.get("action")
    sym = signal.get("symbol", "")
    votes = (signal.get("consensus") or {}).get("votes") or {}
    score = (signal.get("consensus") or {}).get("score")
    unc = signal.get("uncertainty") or {}
    because, despite = [], []

    def add(vote_key, statement):
        v = votes.get(vote_key)
        if v is None or abs(float(v)) <= 0.05:
            return
        line = f"{statement} (vote {float(v):+.2f})"
        (because if float(v) > 0 else despite).append(line)

    tiers = signal.get("mtf_tiers") or {}
    ups, dns = _tier_txt(tiers)
    add("trend", f"Trend: {'/'.join(ups) or 'no'} tiers UP, "
                 f"{'/'.join(dns) or 'none'} DOWN")

    ms = signal.get("market_structure") or {}
    if ms:
        add("structure", f"Structure: {ms.get('bias', 'n/a')} bias, "
                         f"{ms.get('phase', 'n/a')} phase")

    lm = signal.get("liquidity") or {}
    if lm.get("ready"):
        bits = []
        if lm.get("draw"):
            bits.append(f"draw on liquidity {lm['draw']}")
        if lm.get("active_zone"):
            bits.append(f"price in {lm['active_zone']} block")
        delta = (lm.get("cum_delta") or {}).get("bias")
        if delta and delta != "NEUTRAL":
            bits.append(f"delta {delta.lower()} (institutional flow)")
        dom = lm.get("dom") or {}
        if dom.get("live") and dom.get("imbalance") is not None:
            side = "bid" if dom["imbalance"] > 0 else "ask"
            bits.append(f"order book {abs(round(dom['imbalance'] * 100))}% "
                        f"{side}-heavy")
        add("liquidity", f"Liquidity: {', '.join(bits) or 'mapped'}")

    fc = signal.get("forecast") or {}
    if fc.get("median_change_pct") is not None:
        add("forecast", f"Forecast: Chronos median move "
                        f"{fc['median_change_pct']:+.2f}% over the horizon")

    ml = signal.get("ml_ensemble") or {}
    qbits = []
    if ml.get("p_win") is not None:
        qbits.append(f"{ml.get('models_used')} models avg P(win) "
                     f"{round(ml['p_win'] * 100)}%")
    b = signal.get("bayes") or {}
    if b.get("p_success") is not None and b.get("n"):
        qbits.append(f"Bayes {round(b['p_success'] * 100)}% (n={b['n']})")
    if not qbits and signal.get("confidence") is not None:
        qbits.append(f"raw confidence {signal['confidence']}%")
    add("quant", f"Quant: {', '.join(qbits) or 'no model data'}")

    mbits = []
    ft = signal.get("fed_tone") or {}
    if ft.get("label"):
        mbits.append(f"Fed tone {ft['label']}")
    na = signal.get("news_ai") or {}
    if na.get("net") is not None:
        mbits.append(f"AI news {float(na['net']):+.1f}/3 across "
                     f"{na.get('headlines')} headlines")
    cz = signal.get("causal") or {}
    if cz.get("narrative"):
        mbits.append(cz["narrative"])
    add("macro", f"Macro: {'; '.join(mbits) or 'quiet tape'}")

    mc = signal.get("monte_carlo") or {}
    if mc.get("ev_r") is not None:
        because.append(f"Monte Carlo: {mc['paths']:,} paths → TP first "
                       f"{round(mc['p_tp_first'] * 100)}% vs SL "
                       f"{round(mc['p_sl_first'] * 100)}%, EV "
                       f"{mc['ev_r']:+.2f}R")
    cal = signal.get("calendar_policy") or {}
    if cal.get("mode") == "CAUTION":
        despite.append(cal["reason"])

    conf = unc.get("confidence_pct") or signal.get("confidence")
    headline = (f"{action} {sym} — consensus {score}/100"
                + (f", calibrated confidence {conf}% "
                   f"(risk {unc.get('risk')})" if unc else
                   f", confidence {conf}%"))
    return {"headline": headline, "because": because, "despite": despite}
