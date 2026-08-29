"""Broker/server/symbol/session segmented execution-quality evidence
(iter-153). Every measured fill is recorded with its segmentation key so the
10/20 slippage sample thresholds (strategy_guard.MIN_SLIPPAGE_SAMPLES) can be
calibrated FROM EVIDENCE per broker/server/symbol/session instead of a single
global bucket."""
from datetime import datetime, timezone

# UTC trading sessions (FX convention, DST-agnostic on purpose):
# asia 22–07, london 07–12, overlap 12–16, newyork 16–22
def trading_session(dt: datetime) -> str:
    h = dt.astimezone(timezone.utc).hour
    if h < 7 or h >= 22:
        return "asia"
    if h < 12:
        return "london"
    if h < 16:
        return "overlap"
    return "newyork"


def broker_server_of(account: dict) -> str:
    acc = account or {}
    return (str((acc.get("verified_identity") or {}).get("broker_server")
                or (acc.get("expected_identity") or {}).get("broker_server")
                or acc.get("broker_server") or "unknown")).strip() or "unknown"


async def record_fill(db, *, trade: dict, account: dict, symbol: str,
                      side: str, slippage_pips: float,
                      requested_price: float | None,
                      actual_price: float | None) -> dict:
    from pip_utils import base_symbol
    now = datetime.now(timezone.utc)
    doc = {"trade_id": str((trade or {}).get("_id") or ""),
           "account_id": str((account or {}).get("_id") or ""),
           "broker_server": broker_server_of(account),
           "symbol": base_symbol(symbol or "") or (symbol or "").upper(),
           "side": side,
           "session": trading_session(now),
           "true_slippage": bool(requested_price),
           "slippage_pips": round(float(slippage_pips or 0.0), 2),
           "requested_price": requested_price,
           "actual_price": actual_price,
           "ea_version": (account or {}).get("ea_version"),
           "at": now.isoformat()}
    await db.execution_quality.insert_one(dict(doc))
    doc.pop("_id", None)
    return doc


def _pctl(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(p * (len(sorted_vals) - 1) + 0.5))
    return sorted_vals[i]


async def segments(db, days: int = 30) -> dict:
    """Per broker/server/symbol/session distribution + calibration readiness
    against MIN_SLIPPAGE_SAMPLES (only TRUE-slippage fills count)."""
    from datetime import timedelta

    from modules.pamm.strategy_guard import MIN_SLIPPAGE_SAMPLES
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = []
    async for g in db.execution_quality.aggregate([
            {"$match": {"at": {"$gte": since}, "true_slippage": True}},
            {"$group": {"_id": {"broker_server": "$broker_server",
                                "symbol": "$symbol",
                                "session": "$session"},
                        "n": {"$sum": 1},
                        "slips": {"$push": "$slippage_pips"}}},
            {"$sort": {"n": -1}}, {"$limit": 500}]):
        slips = sorted(float(s) for s in g["slips"])
        need_vh = MIN_SLIPPAGE_SAMPLES["VERY_HIGH"]
        need_max = MIN_SLIPPAGE_SAMPLES["MAXIMUM"]
        rows.append({**g["_id"], "samples": g["n"],
                     "median_pips": round(_pctl(slips, 0.5), 2),
                     "p95_pips": round(_pctl(slips, 0.95), 2),
                     "worst_pips": round(slips[-1], 2) if slips else 0.0,
                     "evidence_ready": {
                         "VERY_HIGH": g["n"] >= need_vh,
                         "MAXIMUM": g["n"] >= need_max}})
    total = await db.execution_quality.count_documents(
        {"at": {"$gte": since}})
    return {"days": days, "total_fills": total,
            "true_slippage_segments": rows,
            "thresholds": MIN_SLIPPAGE_SAMPLES,
            "note": "evidence_ready marks segments with enough measured "
                    "fills to trust a p95 at that latency sensitivity — "
                    "calibrate MIN_SLIPPAGE_SAMPLES from these "
                    "distributions once live data accumulates"}
