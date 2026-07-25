"""Phase 1.1 — Decision Validation Framework.

Every trade decision produces FOUR independent verdicts:
  AI Decision → Deterministic Validation → Risk Validation → Execution
A trade proceeds only if all four agree. The quorum is stamped onto every
executed decision in the trade_decisions ledger (bot_runner) so agreement
can be audited forever. Verdicts abstain-approve when their input is absent
(the upstream gate chain remains the enforcement authority; this framework
is the recorded consistency proof).
"""
from datetime import datetime, timezone


def _v(ok: bool, detail: str) -> dict:
    return {"verdict": "approve" if ok else "reject", "detail": detail}


def four_verdicts(signal: dict, cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    sig = signal or {}

    action = sig.get("action")
    conf = sig.get("confidence")
    min_conf = float(cfg.get("min_confidence") or 55)
    ai_ok = action in ("BUY", "SELL") and (conf is None
                                           or float(conf) >= min_conf)
    ai = _v(ai_ok, f"{action} at {conf}% confidence "
                   f"(floor {min_conf}%)" if action in ("BUY", "SELL")
            else f"non-actionable action '{action}'")

    mc = sig.get("monte_carlo") or {}
    ev = mc.get("ev_r_net", mc.get("ev_r"))
    cons = (sig.get("consensus") or {}).get("score")
    det_ok = ((ev is None or float(ev) > 0)
              and (cons is None or float(cons) >= 50))
    det = _v(det_ok,
             f"MC EV {'%+.2f' % float(ev) + 'R' if ev is not None else 'n/a'}"
             f" · consensus {cons if cons is not None else 'n/a'}")

    risk_status = (sig.get("risk_engine") or {}).get("status") \
        or sig.get("risk_status")
    ar_mult = (sig.get("adaptive_risk") or {}).get("multiplier")
    risk_ok = (risk_status not in ("block", "suspend")
               and (ar_mult is None or float(ar_mult) > 0))
    risk = _v(risk_ok, f"risk engine {risk_status or 'ok'} · sizing "
                       f"×{ar_mult if ar_mult is not None else 1}")

    liq_verdict = (sig.get("liquidity") or {}).get("verdict")
    spread_blocked = sig.get("spread_blocked") is True
    exec_ok = liq_verdict not in ("block", "reject") and not spread_blocked
    execu = _v(exec_ok, f"liquidity {liq_verdict or 'ok'} · spread "
                        f"{'BLOCKED' if spread_blocked else 'ok'}")

    verdicts = {"ai": ai, "deterministic": det, "risk": risk,
                "execution": execu}
    return {**verdicts,
            "proceed": all(v["verdict"] == "approve"
                           for v in verdicts.values()),
            "at": datetime.now(timezone.utc).isoformat()}


async def quorum_stats(db, user_id: str, limit: int = 200) -> dict:
    """Agreement audit over recent executed decisions carrying a quorum."""
    rows = await db.trade_decisions.find(
        {"user_id": user_id, "status": "executed",
         "execution.validation_quorum": {"$exists": True}},
        {"execution.validation_quorum": 1, "symbol": 1, "ts": 1}
    ).sort("_id", -1).limit(limit).to_list(limit)
    total = len(rows)
    agreed = 0
    dissent: dict = {}
    recent = []
    for r in rows:
        q = (r.get("execution") or {}).get("validation_quorum") or {}
        if q.get("proceed"):
            agreed += 1
        else:
            for k in ("ai", "deterministic", "risk", "execution"):
                if (q.get(k) or {}).get("verdict") == "reject":
                    dissent[k] = dissent.get(k, 0) + 1
        if len(recent) < 10:
            recent.append({"symbol": r.get("symbol"), "ts": r.get("ts"),
                           "proceed": q.get("proceed"),
                           "verdicts": {k: (q.get(k) or {}).get("verdict")
                                        for k in ("ai", "deterministic",
                                                  "risk", "execution")}})
    return {"total": total, "agreed": agreed,
            "agreement_rate": (round(agreed / total * 100, 1)
                               if total else None),
            "dissent_by_verdict": dissent, "recent": recent}
