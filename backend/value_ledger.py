"""AI Value Ledger — formal accounting of what the AI's interventions
are worth, with every entry labelled by evidential strength:

  observed     — measured on realized closed trades (real money)
  estimated    — model/forecast-derived, clearly not realized
  unobservable — blocked actions have no counterfactual; counted only

The ledger REFUSES to blend the three: observed totals never include
estimates, and unobservable entries never carry a monetary value."""
from datetime import datetime, timedelta, timezone

MAX_DOCS = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def ledger(db, user_id: str, days: int = 30) -> dict:
    days = max(1, min(int(days), 90))
    since = (datetime.now(timezone.utc)
             - timedelta(days=days)).isoformat()
    from intervention_metrics import effectiveness
    eff = await effectiveness(db, user_id, days=days)
    entries = []
    re = eff["reduce_effect"]
    entries.append({
        "source": "meta_reduce_sizing", "effect": "observed",
        "n": re["reduce_cohort"]["n"],
        "value_usd": re["net_usd"],
        "detail": {"saved_on_losers_usd": re["saved_on_losers_usd"],
                   "forgone_on_winners_usd": re["forgone_on_winners_usd"]},
        "basis": "realized pnl delta of size cuts on CLOSED trades "
                 "(saved on losers − forgone on winners)"})
    # execution alpha — forecast slippage on modified executions (estimate)
    est_pips = 0.0
    n_alpha = 0
    async for a in db.execution_alpha_decisions.find(
            {"user_id": user_id, "at": {"$gte": since},
             "mode": {"$ne": "EXECUTE_NOW"}},
            {"forecast.expected_slippage_pips": 1}).limit(MAX_DOCS):
        n_alpha += 1
        try:
            est_pips += float((a.get("forecast") or {}).get(
                "expected_slippage_pips") or 0)
        except (TypeError, ValueError):
            pass
    entries.append({
        "source": "execution_alpha", "effect": "estimated",
        "n": n_alpha, "value_pips_estimated": round(est_pips, 1),
        "basis": "forecast expected slippage on WAIT/REDUCE/SKIP "
                 "executions — model estimate, never realized money"})
    entries.append({
        "source": "uncertainty_hard_gate", "effect": "unobservable",
        "n": eff["meta"]["hard_gates"],
        "basis": "hard-gated skips have no counterfactual outcome"})
    entries.append({
        "source": "meta_skip", "effect": "unobservable",
        "n": eff["skips"]["n"],
        "basis": "meta SKIPs have no counterfactual outcome"})
    entries.append({
        "source": "pretrade_twin_block", "effect": "unobservable",
        "n": eff["twin"]["SKIP"],
        "basis": "twin-blocked trades have no counterfactual outcome"})
    # PAMM risk governance — every guard decision is a hashed Risk
    # Decision Snapshot; blocked new risk has no counterfactual.
    acct_ids = [str(a["_id"]) async for a in db.accounts.find(
        {"user_id": user_id}, {"_id": 1}).limit(200)]
    n_guard = 0
    snap_ids: list = []
    if acct_ids:
        async for d in db.pamm_risk_decisions.find(
                {"account_id": {"$in": acct_ids}, "authorized": False,
                 "at": {"$gte": since}},
                {"snapshot_id": 1}).sort("at", -1).limit(MAX_DOCS):
            n_guard += 1
            if len(snap_ids) < 20:
                snap_ids.append(d.get("snapshot_id"))
    entries.append({
        "source": "pamm_risk_guard", "effect": "unobservable",
        "n": n_guard, "risk_snapshot_ids": snap_ids,
        "basis": "guard-blocked new risk has no counterfactual outcome; "
                 "every decision links to a hashed Risk Decision Snapshot"})
    observed_usd = round(sum(e.get("value_usd") or 0 for e in entries
                             if e["effect"] == "observed"), 2)
    return {"days": days, "entries": entries,
            "observed_total_usd": observed_usd,
            "policy": "observed ≠ estimated ≠ unobservable — the ledger "
                      "never blends evidential strengths",
            "at": _now()}
