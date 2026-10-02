"""Tier 13 — Strategy Genetics: version lineage + did-it-help attribution.

Composes the evolution record from the sources that already govern change:
governed_changes (who approved what), auto_guards (loss-advisor auto
measures), improvement_proposals (research lab), tuning_proposals (nightly
Bayesian tuner: GP fitted on the first 70% of bars, judged on the held-out
last 30% net of costs, plus DSR/PBO selection-bias checks) — and attaches per-strategy-version realized performance
from version-stamped closed trades, so configuration drift is impossible.
"""
from datetime import datetime

from versioning import (EXECUTION_POLICY_VERSION, FEATURE_SCHEMA_VERSION,
                        RISK_POLICY_VERSION, STRATEGY_VERSIONS)


def _iso(v) -> str | None:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v else None


def tuning_why(tp: dict) -> str:
    """Honest label for a tuning proposal: in-sample vs out-of-sample."""
    is_imp = tp.get("improvement")
    if tp.get("oos_improvement") is None:
        # legacy proposals (pre-validation framework) were in-sample only
        return (f"in-sample score gain {is_imp} on the same bars it was "
                f"optimised on (no out-of-sample check, frictionless)")
    dsr = (tp.get("dsr") or {}).get("dsr")
    pbo = (tp.get("pbo") or {}).get("pbo")
    gate = tp.get("gate") or {}
    verdict = ("" if not gate else
               " — gate passed" if gate.get("accepted") else
               f" — gate failed: {', '.join(gate.get('failed') or [])}")
    return (f"in-sample score gain {is_imp}; held-out OOS "
            f"{tp.get('oos_improvement'):+}R net of costs, DSR {dsr}, "
            f"PBO {pbo}{verdict}")[:200]


async def lineage(db, user_id: str) -> dict:
    events = []
    async for g in db.governed_changes.find(
            {"user_id": user_id}).sort("proposed_at", -1).limit(40):
        events.append({
            "ts": _iso(g.get("proposed_at")), "kind": "governance",
            "what": f"{g.get('field')}: {g.get('old_value')} → "
                    f"{g.get('new_value')}",
            "why": str(g.get("detail") or g.get("source") or "")[:200],
            "who": g.get("source"),
            "classification": g.get("classification"),
            "status": g.get("status"), "helped": None})
    async for a in db.auto_guards.find(
            {"user_id": user_id}).sort("created_at", -1).limit(40):
        m = a.get("measure") or {}
        ev = a.get("evidence") or {}
        events.append({
            "ts": _iso(a.get("created_at")), "kind": "auto_guard",
            "what": m.get("title") or m.get("type"),
            "why": str(m.get("rationale") or "")[:200],
            "who": "loss advisor (auto)",
            "status": "active" if a.get("active") else "reverted",
            "helped": ev.get("net")})
    async for p in db.improvement_proposals.find(
            {"user_id": user_id, "status": {"$in": ["accepted", "applied"]}}
            ).sort("created_at", -1).limit(20):
        events.append({
            "ts": _iso(p.get("created_at")), "kind": "research",
            "what": p.get("name"),
            "why": str(p.get("rationale") or "")[:200],
            "who": "research lab", "status": p.get("status"),
            "helped": p.get("delta_vs_baseline")})
    async for tp in db.tuning_proposals.find(
            {"user_id": user_id}).sort("created_at", -1).limit(20):
        events.append({
            "ts": _iso(tp.get("created_at")), "kind": "tuning",
            "what": f"{tp.get('engine')} params v{tp.get('version')}",
            "why": tuning_why(tp),
            "who": "nightly tuner", "status": tp.get("status"),
            # "helped" = held-out evidence only; in-sample gains never count
            "helped": tp.get("oos_improvement")})
    events = [e for e in events if e["ts"]]
    events.sort(key=lambda e: str(e["ts"]), reverse=True)

    perf: dict = {}
    async for t in db.trades.find(
            {"user_id": user_id, "status": "closed", "origin": "auto",
             "pnl": {"$ne": None},
             "versions.strategy_version": {"$exists": True}},
            {"versions": 1, "pnl": 1}):
        v = (t.get("versions") or {}).get("strategy_version")
        e = perf.setdefault(v, {"n": 0, "wins": 0, "pnl": 0.0,
                                "gross_win": 0.0, "gross_loss": 0.0})
        pnl = float(t["pnl"])
        e["n"] += 1
        e["pnl"] += pnl
        if pnl > 0:
            e["wins"] += 1
            e["gross_win"] += pnl
        elif pnl < 0:
            e["gross_loss"] += abs(pnl)

    def _perf(ver):
        p = perf.get(ver)
        if not p:
            return None
        return {"n": p["n"], "wins": p["wins"],
                "win_rate": round(p["wins"] / p["n"] * 100, 1),
                "pnl": round(p["pnl"], 2),
                "profit_factor": (round(p["gross_win"] / p["gross_loss"], 2)
                                  if p["gross_loss"] else None)}

    versions = [{"engine": scope, "current_version": ver,
                 "performance": _perf(ver)}
                for scope, ver in STRATEGY_VERSIONS.items()]
    legacy = [{"version": v, "performance": _perf(v)}
              for v in sorted(perf)
              if v not in STRATEGY_VERSIONS.values()]

    return {"policies": {"risk_policy": RISK_POLICY_VERSION,
                         "execution_policy": EXECUTION_POLICY_VERSION,
                         "feature_schema": FEATURE_SCHEMA_VERSION},
            "versions": versions, "legacy_versions": legacy,
            "events": events[:60],
            "note": ("Every change to the strategy genome is recorded with "
                     "who approved it and whether it helped — measured from "
                     "version-stamped closed trades.")}
