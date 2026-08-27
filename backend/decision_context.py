"""DecisionContext (v59 #1) — every BUY/SELL opportunity receives one
immutable dec_… id and snapshot BEFORE any gate runs; every downstream
stage (meta, memory, portfolio, execution) appends to the same document,
and the executed trade carries decision_id — full reproducibility."""
import logging
import uuid
from datetime import datetime, timezone

logger = logging.getLogger("decision.context")

MAX_STAGES = 40


def new_decision_id() -> str:
    return "dec_" + uuid.uuid4().hex[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def mint(db, user_id: str, signal: dict, cfg: dict | None = None,
               account: dict | None = None) -> str | None:
    """Persist the immutable snapshot; returns decision_id (None on
    failure — the pipeline never blocks on context bookkeeping)."""
    cfg = cfg or {}
    account = account or {}
    decision_id = new_decision_id()
    versions = None
    try:
        from versioning import version_stamp
        versions = version_stamp(signal.get("scope"))
    except Exception:  # noqa: BLE001
        pass
    doc = {
        "decision_id": decision_id,
        "user_id": user_id,
        "symbol": signal.get("symbol"),
        "scope": str(signal.get("scope") or signal.get("origin") or "ai"),
        "market": {k: signal.get(k) for k in
                   ("action", "entry_price", "stop_loss", "take_profit",
                    "confidence", "rr_ratio", "spread", "entry_style")},
        "regime": signal.get("market_state"),
        "versions": versions,
        "account": {"account_id": str(cfg.get("account_id") or "") or None,
                    "equity": cfg.get("_account_equity")
                    or account.get("equity"),
                    "balance": account.get("balance")},
        "broker": {"broker": account.get("broker"),
                   "server": account.get("broker_server")},
        "news": {"net": (signal.get("news_ai") or {}).get("net"),
                 "calendar": (signal.get("calendar_intel") or {}).get(
                     "event")},
        "risk": {"risk_profile": cfg.get("risk_profile"),
                 "max_lot_size": cfg.get("max_lot_size"),
                 "soft_stop_enabled": cfg.get("soft_stop_enabled")},
        "stages": [],
        "at": _now(),
    }
    try:
        await db.decision_contexts.insert_one(doc)
        return decision_id
    except Exception as e:  # noqa: BLE001
        logger.debug("decision context mint failed: %s", e)
        return None


def _trim(payload) -> dict:
    if not isinstance(payload, dict):
        return {"value": str(payload)[:400]}
    out = {}
    for k, v in list(payload.items())[:20]:
        if isinstance(v, (dict, list)):
            out[k] = v if len(str(v)) <= 1500 else str(v)[:1500]
        else:
            out[k] = v
    return out


async def record_stage(db, decision_id: str | None, stage: str,
                       payload: dict | None = None) -> None:
    if not decision_id:
        return
    try:
        await db.decision_contexts.update_one(
            {"decision_id": decision_id,
             f"stages.{MAX_STAGES}": {"$exists": False}},
            {"$push": {"stages": {"stage": stage, "at": _now(),
                                  "detail": _trim(payload or {})}}})
    except Exception as e:  # noqa: BLE001
        logger.debug("record_stage(%s) failed: %s", stage, e)


async def get_decision(db, decision_id: str, user_id: str | None = None,
                       admin: bool = False) -> dict | None:
    q = {"decision_id": decision_id}
    if not admin:
        q["user_id"] = user_id
    doc = await db.decision_contexts.find_one(q, {"_id": 0})
    if not doc:
        return None
    trade = await db.trades.find_one({"decision_id": decision_id})
    if trade:
        doc["trade"] = {"trade_id": str(trade["_id"]),
                        "status": trade.get("status"),
                        "pnl": trade.get("pnl"),
                        "symbol": trade.get("symbol"),
                        "closed_at": trade.get("closed_at")}
        outcome = await db.trade_outcomes.find_one(
            {"trade_id": str(trade["_id"])}, {"_id": 0, "signals": 0})
        if outcome:
            doc["outcome"] = outcome
    return doc


async def ensure_decision_indexes(db) -> None:
    await db.decision_contexts.create_index("decision_id", unique=True)
    await db.decision_contexts.create_index([("user_id", 1), ("at", -1)])
    await db.trades.create_index("decision_id", sparse=True)
