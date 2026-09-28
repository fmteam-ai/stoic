"""Risk Commander deterministic preview (P1 backlog).

Every NL command becomes a stored PROPOSAL whose preview enumerates exactly
what will be touched (which bots, which trades, which risk levels). Confirm
re-computes the preview and refuses to execute when the fingerprint drifted —
what the operator saw is what runs, or nothing runs.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from bson import ObjectId

PROPOSAL_TTL_SEC = 300
CAPITAL_TOUCHING = {"CLOSE_ALL_TRADES", "PANIC_LOCK", "MOVE_STOPS_BREAKEVEN",
                    "ENABLE_BOTS", "SET_RISK_LEVEL"}


def bot_query(user_id: str, target: str) -> dict:
    """Bot scope (r16 P1-01): all | high_risk | bot:<immutable bot_config id>.
    Anything else is REFUSED — a symbol-like token must never widen to 'all bots'."""
    t = str(target or "all")
    q = {"user_id": user_id}
    if t == "all":
        return q
    if t == "high_risk":
        q["risk_level"] = {"$in": ["high", "extreme"]}
        return q
    if t.startswith("bot:") and ObjectId.is_valid(t[4:]):
        q["_id"] = ObjectId(t[4:])
        return q
    raise ValueError(f"invalid bot target {target!r}")


def trade_query(user_id: str, target: str, statuses: list) -> dict:
    """Trade scope: all | normalised SYMBOL."""
    t = str(target or "all")
    q = {"user_id": user_id, "status": {"$in": statuses}}
    if t == "all":
        return q
    if t.startswith("bot:"):
        raise ValueError(f"trade actions take a symbol, not {target!r}")
    q["symbol"] = t.upper()
    return q


_bot_query = bot_query
_trade_query = trade_query


def inventory_hash(ids: list) -> str:
    return hashlib.sha256(json.dumps(sorted(str(i) for i in ids)).encode()).hexdigest()[:16]


async def _bot_rows(db, q: dict) -> list:
    docs = await db.bot_configs.find(q).to_list(length=200)
    acct_ids = [ObjectId(d["account_id"]) for d in docs
                if d.get("account_id") and ObjectId.is_valid(d["account_id"])]
    labels = {}
    if acct_ids:
        async for a in db.accounts.find({"_id": {"$in": acct_ids}},
                                        {"display_name": 1, "label": 1, "broker": 1}):
            labels[str(a["_id"])] = a.get("display_name") or a.get("label") or a.get("broker") or str(a["_id"])[-6:]
    return [{"id": str(d["_id"]),
             "account": labels.get(str(d.get("account_id")), "default"),
             "risk_level": d.get("risk_level"),
             "active": bool(d.get("active")),
             "preset": d.get("active_preset")} for d in docs]


async def _trade_rows(db, q: dict) -> list:
    docs = await db.trades.find(q, {"symbol": 1, "action": 1, "entry_price": 1,
                                    "live_pnl": 1, "lot_size": 1, "status": 1}).to_list(length=500)
    return [{"id": str(t["_id"]), "symbol": t.get("symbol"), "side": t.get("action"),
             "entry": t.get("entry_price"), "live_pnl": t.get("live_pnl"),
             "lots": t.get("lot_size"), "status": t.get("status")} for t in docs]


async def preview_action(db, user_id: str, act: dict) -> dict:
    a_type = str(act.get("type") or "").upper()
    target = act.get("target") or "all"
    params = act.get("params") or {}
    out = {"type": a_type, "target": target, "capital_touching": a_type in CAPITAL_TOUCHING}
    if a_type in ("DISABLE_BOTS", "ENABLE_BOTS"):
        want = a_type == "ENABLE_BOTS"
        rows = [r for r in await _bot_rows(db, _bot_query(user_id, target)) if r["active"] != want]
        out.update(effect=f"{'Enable' if want else 'Disable'} {len(rows)} bot(s)",
                   bots=rows, count=len(rows), ids=[r["id"] for r in rows])
    elif a_type == "MOVE_STOPS_BREAKEVEN":
        rows = await _trade_rows(db, _trade_query(user_id, target, ["open"]))
        out.update(effect=f"Move stop-loss to entry on {len(rows)} open trade(s)",
                   trades=rows, count=len(rows), ids=[r["id"] for r in rows])
    elif a_type == "CLOSE_ALL_TRADES":
        rows = await _trade_rows(db, _trade_query(user_id, target, ["open", "pending"]))
        pnl = round(sum(float(r["live_pnl"] or 0) for r in rows), 2)
        out.update(effect=f"Close {len(rows)} trade(s) · live P&L {pnl:+.2f}",
                   trades=rows, count=len(rows), live_pnl=pnl, ids=[r["id"] for r in rows])
    elif a_type == "SET_RISK_LEVEL":
        level = params.get("risk_level", "low")
        rows = [dict(r, to=level) for r in await _bot_rows(db, _bot_query(user_id, target))]
        changed = [r for r in rows if r["risk_level"] != level]
        out.update(effect=f"Set risk → {level.upper()} on {len(changed)} bot(s) ({target})",
                   bots=changed, count=len(changed), risk_level=level,
                   ids=[f"{r['id']}:{r['risk_level']}" for r in changed])
    elif a_type == "PANIC_LOCK":
        bots = [r for r in await _bot_rows(db, {"user_id": user_id}) if r["active"]]
        trades = await _trade_rows(db, _trade_query(user_id, "all", ["open", "pending"]))
        out.update(target="all", effect=f"PANIC — account-wide: disable {len(bots)} bot(s) and close {len(trades)} trade(s)",
                   bots=bots, trades=trades, count=len(bots) + len(trades),
                   ids=[r["id"] for r in bots] + [r["id"] for r in trades])
    elif a_type == "SET_CONDITIONAL_TRIGGER":
        spec = {"symbol": str(params.get("symbol") or "BTCUSD").upper(),
                "condition": params.get("condition", "drop"),
                "threshold_pct": float(params.get("threshold_pct", 3.0)),
                "then": [str(t.get("type") or "").upper() for t in (params.get("then") or [])]}
        out.update(effect=(f"Arm trigger: {spec['symbol']} {spec['condition']} "
                           f"{spec['threshold_pct']}% → {', '.join(spec['then']) or 'no actions'}"),
                   trigger=spec, count=1, ids=[json.dumps(spec, sort_keys=True)])
    else:
        out.update(effect=f"Unknown action {a_type}", count=0, ids=[])
    # r16 P1-01 — the stored proposal carries the RESOLVED immutable ids and their hash
    out["resolved_ids"] = list(out.get("ids") or [])
    out["inventory_hash"] = inventory_hash(out["resolved_ids"])
    return out


def fingerprint(previews: list) -> str:
    canon = [{"type": p["type"], "target": p["target"], "ids": sorted(p.get("ids") or [])}
             for p in previews]
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


async def build_preview(db, user_id: str, actions: list) -> dict:
    items = [await preview_action(db, user_id, a) for a in actions]
    return {"actions": items,
            "capital_touching": any(i["capital_touching"] for i in items),
            "total_effects": sum(i.get("count") or 0 for i in items),
            "fingerprint": fingerprint(items)}


async def store_proposal(db, user_id: str, prompt: str, actions: list, preview: dict) -> dict:
    now = datetime.now(timezone.utc)
    doc = {"user_id": user_id, "prompt": prompt, "actions": actions,
           "preview": preview, "status": "pending",
           "created_at": now.isoformat(),
           "expires_at": (now + timedelta(seconds=PROPOSAL_TTL_SEC)).isoformat()}
    r = await db.nl_proposals.insert_one(doc)
    doc["id"] = str(r.inserted_id)
    return doc


def is_expired(doc: dict) -> bool:
    return datetime.fromisoformat(doc["expires_at"]) <= datetime.now(timezone.utc)
