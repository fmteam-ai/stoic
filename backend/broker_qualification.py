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

    # Correction #5 — spec checks from EA-reported evidence (v1.48 specs,
    # v1.54 full contract specs + broker time / session facts).
    specs = account.get("symbol_specs") or {}

    def _spec_vals(field):
        return {s: sp.get(field) for s, sp in specs.items()
                if isinstance(sp, dict) and sp.get(field) is not None}
    stops = _spec_vals("stops_level_points")
    checks["stop_restrictions"] = (
        {"status": "observed",
         "value": f"max {max(stops.values()):.0f} pts over "
                  f"{len(stops)} symbols"}
        if stops else {"status": "not_reported", "value": None,
                       "note": "requires EA ≥ v1.48 symbol specs"})
    freezes = _spec_vals("freeze_level_points")
    checks["freeze_levels"] = (
        {"status": "observed",
         "value": f"max {max(freezes.values()):.0f} pts over "
                  f"{len(freezes)} symbols"}
        if freezes else {"status": "not_reported", "value": None,
                         "note": "requires EA ≥ v1.48 symbol specs"})
    full_spec = [s for s, sp in specs.items()
                 if isinstance(sp, dict)
                 and sp.get("tick_value") is not None
                 and sp.get("contract_size") is not None]
    checks["symbol_specs"] = (
        {"status": "observed",
         "value": f"{len(specs)} symbols, {len(full_spec)} with full "
                  f"contract specs"}
        if full_spec else {"status": "not_reported", "value": None,
                           "note": "requires EA ≥ v1.54 full contract "
                                   "specs (tick value, contract size)"})
    bt = account.get("broker_time_info") or {}
    off = bt.get("server_gmt_offset_sec")
    checks["dst_handling"] = (
        {"status": "observed",
         "value": f"server GMT{float(off) / 3600:+.1f}h · "
                  f"{len(bt.get('trade_sessions_today') or [])} trade "
                  f"session(s) reported"}
        if off is not None else
        {"status": "not_reported", "value": None,
         "note": "requires EA ≥ v1.54 broker time report"})

    SPEC_CHECKS = ("stop_restrictions", "freeze_levels", "symbol_specs",
                   "dst_handling")
    missing_specs = [k for k in SPEC_CHECKS
                     if checks[k]["status"] != "observed"]

    score = intel.get("score")
    observed = sum(1 for c in checks.values() if c["status"] == "observed")
    if n_deals < 20 or score is None:
        tier = "PROVISIONAL"
        detail = f"needs ≥20 deals ({n_deals}) and an intel score"
    elif float(score) >= 80 and not missing_specs:
        tier = "CERTIFIED"
        detail = f"intel {score} over {n_deals} deals · {observed}/11 checks observed"
    elif float(score) >= 80:
        tier = "ACCEPTABLE"
        detail = (f"intel {score} — CERTIFIED withheld: spec reporting "
                  f"incomplete ({', '.join(missing_specs)}) — update EA "
                  f"to v1.54")
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
                     "behavior counts. Full CERTIFIED status requires the "
                     "EA v1.54 spec report (stop/freeze levels, contract "
                     "specs, broker time/DST).")}
