"""Phase 2.1 — Broker Qualification Matrix.

Validates every connected broker/account from OBSERVED evidence (deals,
intel scores, heartbeats, account facts) and maintains a certification
record per broker in `broker_certifications`. Checks the EA does not yet
report (stop restrictions, freeze levels, symbol specs, DST) are surfaced
as `not_reported` so the matrix is honest about coverage.
"""
from datetime import datetime, timezone

CHECKS = ["netting_hedging", "partial_fills", "slippage", "latency",
          "reject_rate", "spread", "commission_model", "stop_restrictions",
          "freeze_levels", "symbol_specs", "dst_handling"]


async def qualify_account(db, account: dict) -> dict:
    acc_id = str(account["_id"])
    checks: dict = {}

    checks["netting_hedging"] = {
        "status": "observed" if account.get("account_type") else
        "not_reported",
        "value": account.get("account_type")}

    # partial fills — multiple 'in' deals against one ticket
    pipeline = [
        {"$match": {"account_id": acc_id, "deal_entry": "in"}},
        {"$group": {"_id": "$mt5_ticket", "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}}, {"$limit": 1}]
    partial = await db.broker_deals.aggregate(pipeline).to_list(1)
    n_deals = await db.broker_deals.count_documents({"account_id": acc_id})
    checks["partial_fills"] = {
        "status": "observed" if n_deals else "not_reported",
        "value": bool(partial) if n_deals else None}

    intel = await db.broker_intel_scores.find_one(
        {"account_id": acc_id}, sort=[("at", -1)]) or {}
    comps = intel.get("components") or {}

    def _comp(k):
        v = comps.get(k)
        if isinstance(v, dict):
            v = v.get("score")
        return {"status": "observed" if v is not None else "not_reported",
                "value": round(float(v), 1) if v is not None else None}
    checks["slippage"] = _comp("slippage")
    checks["latency"] = _comp("latency")
    checks["reject_rate"] = _comp("rejects") if "rejects" in comps \
        else _comp("reject_rate")
    checks["spread"] = _comp("spread")

    # commission model from observed deals
    agg = await db.broker_deals.aggregate([
        {"$match": {"account_id": acc_id, "deal_entry": "in"}},
        {"$group": {"_id": None, "n": {"$sum": 1},
                    "commission": {"$sum": {"$abs": "$commission"}},
                    "lots": {"$sum": "$lots"}}}]).to_list(1)
    if agg and agg[0]["n"]:
        a = agg[0]
        per_lot = round(a["commission"] / a["lots"], 2) if a["lots"] else 0
        checks["commission_model"] = {
            "status": "observed",
            "value": ("spread-only" if a["commission"] == 0
                      else f"~${per_lot}/lot commission")}
    else:
        checks["commission_model"] = {"status": "not_reported",
                                      "value": None}

    for k in ("stop_restrictions", "freeze_levels", "symbol_specs",
              "dst_handling"):
        checks[k] = {"status": "not_reported", "value": None,
                     "note": "EA does not report this yet"}

    score = intel.get("score")
    observed = sum(1 for c in checks.values() if c["status"] == "observed")
    if n_deals < 20 or score is None:
        tier = "PROVISIONAL"
        detail = f"needs ≥20 deals ({n_deals}) and an intel score"
    elif float(score) >= 80:
        tier = "CERTIFIED"
        detail = f"intel {score} over {n_deals} deals · {observed}/11 checks observed"
    elif float(score) >= 55:
        tier = "ACCEPTABLE"
        detail = f"intel {score} — adequate, monitor"
    else:
        tier = "DEGRADED"
        detail = f"intel {score} < 55 — poor execution"

    cert = {"account_id": acc_id,
            "broker": account.get("broker"),
            "server": account.get("server"),
            "label": account.get("label"),
            "checks": checks, "tier": tier, "detail": detail,
            "deals_observed": n_deals,
            "intel_score": score,
            "at": datetime.now(timezone.utc)}
    await db.broker_certifications.update_one(
        {"account_id": acc_id}, {"$set": cert}, upsert=True)
    cert["at"] = cert["at"].isoformat()
    return cert


async def qualification_matrix(db, user_id: str) -> dict:
    rows = []
    async for a in db.accounts.find({"user_id": user_id,
                                     "status": {"$ne": "deleted"}}):
        rows.append(await qualify_account(db, a))
    return {"accounts": rows, "checks": CHECKS,
            "note": ("Certification is evidence-based: only observed "
                     "behavior counts. 'not_reported' checks require EA "
                     "spec reporting (backlog).")}
