from datetime import datetime, timezone, timedelta
from typing import List, Literal
from fastapi import APIRouter, Depends, HTTPException, Request
from bson import ObjectId
from pydantic import BaseModel, Field

from auth import get_current_user, generate_bridge_token, verify_password
from database import get_db
from models import AccountCreate, AccountCredsUpdate
from secrets_vault import encrypt as vault_encrypt, decrypt as vault_decrypt
from route_utils import parse_object_id
from account_limits import (
    get_broker_breakdown,
    check_can_add_live_account,
)
from broker_presets import BROKER_PRESETS

router = APIRouter(prefix="/accounts", tags=["accounts"])


class ImportPosition(BaseModel):
    ticket: int
    symbol: str
    type: Literal["BUY", "SELL"]
    volume: float = Field(gt=0)
    price_open: float = Field(gt=0)
    sl: float = 0.0
    tp: float = 0.0


class ImportPositionsRequest(BaseModel):
    positions: List[ImportPosition]


class AccountUpdate(BaseModel):
    """iter-137 · Multi-account management — editable metadata."""
    label: str | None = None
    group: str | None = None
    trading_enabled: bool | None = None


def _iso_days_ago(days: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    creds = doc.pop("creds", {}) or {}
    doc["has_investor_password"] = bool(creds.get("investor"))
    doc["has_master_password"] = bool(creds.get("master"))
    # SEC hardening — the bridge token is a secret; never bulk-return it.
    # The owner fetches it on demand via GET /{id}/bridge-token.
    doc["has_bridge_token"] = bool(doc.pop("bridge_token", None))
    doc.pop("bridge_token_prev", None)
    return doc


@router.get("")
async def list_accounts(user=Depends(get_current_user)):
    db = get_db()
    cursor = db.accounts.find({"user_id": user["id"]}).sort("created_at", -1)
    docs = await cursor.to_list(length=100)
    return [_serialize(d) for d in docs]


@router.get("/limits")
async def account_limits(user=Depends(get_current_user)):
    """Return the user's broker/account-slot usage and the current global caps."""
    db = get_db()
    return await get_broker_breakdown(db, user["id"])


@router.get("/overview")
async def accounts_overview(user=Depends(get_current_user)):
    """iter-137 · Aggregate multi-account portfolio view.

    Totals (balance/equity/floating), per-account realized P&L windows
    (today / 7d / 30d, from closed trades), connection + trading state,
    and the distinct group labels in use.
    """
    db = get_db()
    accounts = await db.accounts.find({"user_id": user["id"]}).sort("created_at", -1).to_list(length=100)
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    hb_cutoff = (now - timedelta(minutes=5)).isoformat()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    d7 = _iso_days_ago(7)
    d30 = _iso_days_ago(30)

    # One query for all closed trades in the last 30 days, bucketed in python.
    trades = await db.trades.find(
        {"user_id": user["id"], "status": "closed", "closed_at": {"$gte": d30}},
        {"account_id": 1, "pnl": 1, "closed_at": 1},
    ).to_list(length=20000)
    pnl = {}  # account_id -> {today, d7, d30}
    for t in trades:
        aid = t.get("account_id") or ""
        p = float(t.get("pnl") or 0)
        ca = t.get("closed_at") or ""
        b = pnl.setdefault(aid, {"today": 0.0, "d7": 0.0, "d30": 0.0})
        b["d30"] += p
        if ca >= d7:
            b["d7"] += p
        if ca >= today_start:
            b["today"] += p

    out_accounts, groups = [], set()
    tot = {"balance": 0.0, "equity": 0.0, "connected": 0, "trading_enabled": 0,
           "pnl_today": 0.0, "pnl_7d": 0.0, "pnl_30d": 0.0, "open_positions": 0}
    for a in accounts:
        aid = str(a["_id"])
        connected = (a.get("mode") == "paper") or bool(
            a.get("last_heartbeat") and a["last_heartbeat"] >= hb_cutoff)
        enabled = a.get("trading_enabled") is not False
        grp = (a.get("group") or "").strip()
        if grp:
            groups.add(grp)
        b = pnl.get(aid, {"today": 0.0, "d7": 0.0, "d30": 0.0})
        bal, eq = float(a.get("balance") or 0), float(a.get("equity") or 0)
        out_accounts.append({
            "id": aid, "label": a.get("label"), "broker": a.get("broker"),
            "account_number": a.get("account_number"), "mode": a.get("mode"),
            "group": grp or None, "trading_enabled": enabled, "connected": connected,
            "balance": a.get("balance"), "equity": a.get("equity"),
            "open_positions": a.get("open_positions") or 0,
            "last_heartbeat": a.get("last_heartbeat"),
            "pnl_today": round(b["today"], 2), "pnl_7d": round(b["d7"], 2),
            "pnl_30d": round(b["d30"], 2),
        })
        tot["balance"] += bal
        tot["equity"] += eq
        tot["connected"] += 1 if connected else 0
        tot["trading_enabled"] += 1 if enabled else 0
        tot["open_positions"] += a.get("open_positions") or 0
        tot["pnl_today"] += b["today"]
        tot["pnl_7d"] += b["d7"]
        tot["pnl_30d"] += b["d30"]
    for k in ("balance", "equity", "pnl_today", "pnl_7d", "pnl_30d"):
        tot[k] = round(tot[k], 2)
    tot["floating"] = round(tot["equity"] - tot["balance"], 2)
    tot["accounts"] = len(accounts)
    return {"totals": tot, "accounts": out_accounts, "groups": sorted(groups)}


@router.get("/equity-curve")
async def accounts_equity_curve(days: int = 30, user=Depends(get_current_user)):
    """iter-137 · Combined cumulative realized P&L series across all accounts.

    Daily buckets from closed-trade P&L; each point carries the combined
    running total plus a running total per account (key `a_<id>`).
    """
    days = max(1, min(days, 365))
    db = get_db()
    accounts = await db.accounts.find(
        {"user_id": user["id"]}, {"label": 1}).to_list(length=100)
    since = _iso_days_ago(days)
    trades = await db.trades.find(
        {"user_id": user["id"], "status": "closed", "closed_at": {"$gte": since}},
        {"account_id": 1, "pnl": 1, "closed_at": 1},
    ).to_list(length=50000)

    from datetime import timedelta
    daily = {}  # date -> {account_id -> pnl}
    for t in trades:
        d = (t.get("closed_at") or "")[:10]
        if not d:
            continue
        daily.setdefault(d, {})
        aid = t.get("account_id") or "unknown"
        daily[d][aid] = daily[d].get(aid, 0.0) + float(t.get("pnl") or 0)

    acct_ids = [str(a["_id"]) for a in accounts]
    running = {aid: 0.0 for aid in acct_ids}
    total_running = 0.0
    series = []
    start = (datetime.now(timezone.utc) - timedelta(days=days - 1)).date()
    today = datetime.now(timezone.utc).date()
    day = start
    while day <= today:
        key = day.isoformat()
        buckets = daily.get(key, {})
        for aid, p in buckets.items():
            if aid in running:
                running[aid] += p
                total_running += p
        point = {"date": key, "total": round(total_running, 2)}
        for aid in acct_ids:
            point[f"a_{aid}"] = round(running[aid], 2)
        series.append(point)
        day += timedelta(days=1)
    return {
        "days": days,
        "accounts": [{"id": str(a["_id"]), "label": a.get("label")} for a in accounts],
        "series": series,
    }


@router.get("/broker-matrix")
async def broker_matrix(user=Depends(get_current_user)):
    """Iter-159 · per-broker compatibility matrix — capabilities, quirks
    derived from live account data, and learned execution stats."""
    db = get_db()
    rows = {}
    async for a in db.accounts.find(
            {"user_id": user["id"], "status": {"$ne": "deleted"}},
            {"broker": 1, "mode": 1, "account_type": 1, "ea_version": 1,
             "available_symbols": 1, "symbol_specs": 1, "block_retcode": 1,
             "block_retcode_label": 1, "demo_certified_at": 1}):
        b = (a.get("broker") or "").strip() or "Unknown"
        r = rows.setdefault(b, {"broker": b, "accounts": 0, "modes": set(),
                                "account_types": set(), "ea_versions": set(),
                                "symbols": 0, "suffixed": False,
                                "stops_syms": 0, "freeze_syms": 0,
                                "retcodes": [], "certified": False})
        r["accounts"] += 1
        r["modes"].add(str(a.get("mode") or "live"))
        if a.get("account_type"):
            r["account_types"].add(str(a["account_type"]).lower())
        if a.get("ea_version"):
            r["ea_versions"].add(str(a["ea_version"]))
        syms = a.get("available_symbols") or []
        r["symbols"] = max(r["symbols"], len(syms))
        if any(("." in s or "_" in s or (s and s[-1].islower()))
               for s in syms if isinstance(s, str)):
            r["suffixed"] = True
        specs = a.get("symbol_specs") or {}
        r["stops_syms"] = max(r["stops_syms"], sum(
            1 for s in specs.values() if (s.get("stops_level_points") or 0) > 0))
        r["freeze_syms"] = max(r["freeze_syms"], sum(
            1 for s in specs.values() if (s.get("freeze_level_points") or 0) > 0))
        if a.get("block_retcode"):
            r["retcodes"].append({"retcode": a["block_retcode"],
                                  "label": a.get("block_retcode_label")})
        if a.get("demo_certified_at"):
            r["certified"] = True

    out = []
    for b, r in rows.items():
        ver = max(r["ea_versions"]) if r["ea_versions"] else None
        quirks = []
        if "netting" in r["account_types"]:
            quirks.append("Netting mode — same-symbol positions merge; "
                          "deal-level volume truth applies")
        if "hedging" in r["account_types"]:
            quirks.append("Hedging mode — independent tickets per position")
        if r["suffixed"]:
            quirks.append("Suffixed symbol names — symbol mapping normalises "
                          "(e.g. XAUUSD.m → XAUUSD)")
        if r["stops_syms"]:
            quirks.append(f"Min stop distance enforced on {r['stops_syms']} "
                          "symbols (stops_level)")
        if r["freeze_syms"]:
            quirks.append(f"Freeze level on {r['freeze_syms']} symbols — "
                          "no SL/TP edits near market")
        if r["retcodes"]:
            quirks.append("Recent broker rejections recorded — see retcodes")
        learned = None
        try:
            from scalp.broker_stats import summary
            learned = await summary(db, b)
        except Exception:
            pass
        out.append({"broker": b, "accounts": r["accounts"],
                    "modes": sorted(r["modes"]),
                    "account_types": sorted(r["account_types"]) or None,
                    "ea_version": ver,
                    "ordercheck": bool(ver) and str(ver) >= "1.50",
                    "symbols_mapped": r["symbols"],
                    "certified": r["certified"],
                    "quirks": quirks,
                    "recent_retcodes": r["retcodes"][:5],
                    "learned": learned})
    return {"brokers": sorted(out, key=lambda x: -x["accounts"])}


@router.get("/broker-comparison")
async def broker_comparison(days: int = 30, user=Depends(get_current_user)):
    """iter-138 · Phase H-B — side-by-side broker comparison.

    Groups the user's accounts + bot trades by broker and computes:
      execution quality  — avg/worst |slippage|, median fill latency, fail rate
      profitability      — net P&L, win rate, profit factor, avg win/loss
      accounts           — count, combined equity, connected count
    Bot trades only (origin=auto) so brokers are compared on identical signals.
    """
    days = max(1, min(days, 365))
    db = get_db()
    since = _iso_days_ago(days)
    from datetime import timedelta
    hb_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

    accounts = await db.accounts.find({"user_id": user["id"]}).to_list(length=100)
    acct_broker = {str(a["_id"]): (a.get("broker") or "").strip() or
                   ("Paper" if a.get("mode") == "paper" else "Unknown")
                   for a in accounts}

    trades = await db.trades.find(
        {"user_id": user["id"], "origin": "auto",
         "$or": [{"closed_at": {"$gte": since}},
                 {"opened_at": {"$gte": since}},
                 {"_dispatched_at": {"$gte": since}}]},
        {"account_id": 1, "broker": 1, "status": 1, "pnl": 1, "closed_at": 1,
         "slippage_pips": 1, "requested_price": 1, "_dispatched_at": 1,
         "live_at": 1, "opened_at": 1, "symbol": 1},
    ).to_list(length=50000)

    def _broker_of(t):
        b = (t.get("broker") or "").strip()
        return b or acct_broker.get(t.get("account_id") or "", "Unknown")

    def _latency_s(t):
        # Dispatch latency: trade creation → EA order pickup (_dispatched_at).
        try:
            d0 = datetime.fromisoformat(t["opened_at"])
            d1 = datetime.fromisoformat(t["_dispatched_at"])
            s = (d1 - d0).total_seconds()
            return s if 0 <= s <= 120 else None
        except Exception:
            return None

    buckets = {}
    for t in trades:
        buckets.setdefault(_broker_of(t), []).append(t)

    brokers = []
    for name in sorted(buckets.keys() | {b for b in acct_broker.values()}):
        rows = buckets.get(name, [])
        closed = [t for t in rows if t.get("status") == "closed"]
        failed = [t for t in rows if t.get("status") == "failed"]
        filled = [t for t in rows if t.get("live_at") or t.get("status") == "closed"]
        # True slippage requires EA v1.40+ requested_price — legacy
        # signal-vs-fill deltas are latency drift, never slippage (PRD rule).
        slips = [abs(float(t["slippage_pips"])) for t in closed
                 if t.get("slippage_pips") is not None and t.get("requested_price")]
        lats = sorted(s for s in (_latency_s(t) for t in rows
                                  if t.get("_dispatched_at") and t.get("opened_at"))
                      if s is not None)
        wins = [float(t["pnl"]) for t in closed if (t.get("pnl") or 0) > 0]
        losses = [float(t["pnl"]) for t in closed if (t.get("pnl") or 0) < 0]
        gross_win, gross_loss = sum(wins), abs(sum(losses))
        net = sum(float(t.get("pnl") or 0) for t in closed)
        attempts = len(filled) + len(failed)

        b_accounts = [a for a in accounts if acct_broker[str(a["_id"])] == name]
        brokers.append({
            "broker": name,
            "execution": {
                "avg_slippage_pips": round(sum(slips) / len(slips), 2) if slips else None,
                "worst_slippage_pips": round(max(slips), 2) if slips else None,
                "slippage_samples": len(slips),
                "median_fill_latency_s": round(lats[len(lats) // 2], 1) if lats else None,
                "latency_samples": len(lats),
                "latency_kind": "dispatch",
                "failed_orders": len(failed),
                "fail_rate_pct": round(100 * len(failed) / attempts, 1) if attempts else None,
            },
            "pnl": {
                "trades": len(closed),
                "wins": len(wins), "losses": len(losses),
                "win_rate": round(100 * len(wins) / len(closed), 1) if closed else None,
                "net_pnl": round(net, 2),
                "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else (None if not wins else 99.0),
                "avg_win": round(gross_win / len(wins), 2) if wins else None,
                "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
            },
            "accounts": {
                "count": len(b_accounts),
                "equity": round(sum(float(a.get("equity") or 0) for a in b_accounts), 2),
                "connected": sum(1 for a in b_accounts if a.get("mode") == "paper"
                                 or (a.get("last_heartbeat") or "") >= hb_cutoff),
            },
        })
    # Brokers with neither accounts nor trades add noise — drop empty shells.
    brokers = [b for b in brokers
               if b["accounts"]["count"] > 0 or b["pnl"]["trades"] > 0
               or b["execution"]["failed_orders"] > 0]
    return {"days": days, "brokers": brokers}


@router.patch("/{account_id}")
async def update_account(account_id: str, payload: AccountUpdate,
                         user=Depends(get_current_user)):
    """iter-137 · Update account metadata: label, group, trading_enabled."""
    updates = {}
    if payload.label is not None:
        if not payload.label.strip():
            raise HTTPException(status_code=400, detail="Label cannot be empty")
        updates["label"] = payload.label.strip()
    if payload.group is not None:
        updates["group"] = payload.group.strip()
    if payload.trading_enabled is not None:
        updates["trading_enabled"] = payload.trading_enabled
    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")
    db = get_db()
    result = await db.accounts.update_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]},
        {"$set": updates},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    doc = await db.accounts.find_one({"_id": parse_object_id(account_id, "Account")})
    return _serialize(doc)


@router.get("/broker-presets")
async def broker_presets(user=Depends(get_current_user)):
    """List of commonly-used MT5 brokers with typical server names.

    `user` dependency is intentional — only authenticated users should be
    able to enumerate presets (keeps it lightly gated for analytics).
    """
    _ = user  # silence linter — auth dependency
    return {"presets": BROKER_PRESETS}


@router.post("")
async def create_account(payload: AccountCreate, user=Depends(get_current_user)):
    db = get_db()
    is_paper = payload.mode == "paper"

    # iter-60 tier-based account quota (Starter=1, Pro=3, Elite=unlimited).
    # Paper accounts count too — a Starter user shouldn't be able to spawn
    # dozens of paper bots to game the cap. Admins bypass entirely.
    from entitlements import enforce_account_quota
    current_count = await db.accounts.count_documents({"user_id": user["id"]})
    await enforce_account_quota(user, current_count)

    # Per-user limits — paper accounts are exempt (sandbox).
    if not is_paper:
        allowed, err = await check_can_add_live_account(db, user["id"], payload.broker)
        if not allowed:
            raise HTTPException(status_code=403, detail=err)

    # iter-83 · Uniqueness guard. The same (broker, account_number) pair must
    # not exist twice — when the same EA's heartbeats hit two account docs
    # bot_configs split across them and the bot can double-fire trades on the
    # real broker account (see STARTRADER #1610095364 duplicate observed in
    # prod). Paper accounts are exempt (their `account_number` is a synthetic
    # `PAPER1/PAPER2/...` slug per user). Admins are NOT exempt — there's no
    # legitimate reason to register the same live account twice.
    if not is_paper and payload.account_number:
        acct_num = str(payload.account_number).strip()
        # Within this user: hard block — re-adding their own broker account.
        own_dup = await db.accounts.find_one({
            "user_id": user["id"],
            "broker": payload.broker,
            "account_number": acct_num,
            "mode": {"$ne": "paper"},
        })
        if own_dup:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_account",
                    "message": (
                        f"You already have {payload.broker} account #{acct_num} "
                        "connected. To re-pair the EA, rotate the bridge token "
                        "from the existing account's menu instead of re-adding."
                    ),
                    "existing_account_id": str(own_dup["_id"]),
                },
            )
        # Cross-user: only block if the OTHER user's EA is actively heartbeating
        # (within the last 24h). A long-abandoned record shouldn't lock the
        # broker account out for a new owner who legitimately took it over.
        from datetime import timedelta
        active_cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cross_dup = await db.accounts.find_one({
            "user_id": {"$ne": user["id"]},
            "broker": payload.broker,
            "account_number": acct_num,
            "mode": {"$ne": "paper"},
            "last_heartbeat": {"$gte": active_cutoff},
        })
        if cross_dup:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "account_in_use",
                    "message": (
                        f"{payload.broker} account #{acct_num} is already linked "
                        "to another active STOIC user. If this account belongs "
                        "to you, contact support to reclaim it."
                    ),
                },
            )

    starting = float(payload.initial_balance) if is_paper else 0.0

    creds = {}
    if not is_paper:
        if payload.investor_password:
            creds["investor"] = vault_encrypt(payload.investor_password)
        if payload.master_password:
            creds["master"] = vault_encrypt(payload.master_password)

    doc = {
        "user_id": user["id"],
        "label": payload.label,
        # iter-125 identity structure — display_name is PRESENTATION ONLY;
        # expected_identity is the user's registration claim; the
        # verified_identity is stamped later by a verified EA heartbeat.
        "display_name": payload.label,
        "expected_identity": {
            "account_number": payload.account_number,
            "broker_server": "paper-virtual" if is_paper else payload.server,
        },
        "broker": "INTERNAL_PAPER" if is_paper else payload.broker,
        "server": "paper-virtual" if is_paper else payload.server,
        "account_number": payload.account_number,
        "account_type": payload.account_type,
        "base_currency": payload.base_currency,
        "mode": payload.mode,
        "bridge_token": generate_bridge_token(),  # unused for paper but harmless
        "status": "connected" if is_paper else "disconnected",
        "balance": starting,
        "equity": starting,
        "initial_balance": starting,
        "last_heartbeat": datetime.now(timezone.utc).isoformat() if is_paper else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "creds": creds,
    }
    result = await db.accounts.insert_one(doc)
    doc["_id"] = result.inserted_id
    return _serialize(doc)


@router.delete("/{account_id}")
async def delete_account(account_id: str, force: bool = False,
                         user=Depends(get_current_user)):
    """Delete an account.

    Refuses if the account has any non-terminal trades attached (open or
    pending). This prevents the orphan-trades-on-deleted-account class of
    bug. The frontend should:
      1. Call POST /api/trades/reconcile to sync stale state
      2. Manually close any genuinely-open trades
      3. Retry DELETE
    Pass `?force=true` to override (admin / cleanup escape hatch).
    """
    db = get_db()
    if not force:
        non_terminal = await db.trades.count_documents({
            "account_id": account_id,
            "user_id": user["id"],
            "status": {"$in": ["open", "pending"]},
        })
        if non_terminal > 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot delete account: {non_terminal} non-terminal "
                    "trade(s) still attached. Click SYNC WITH BROKER on the "
                    "Trades page to clear orphans, or close them manually first. "
                    "Pass ?force=true to override."
                ),
            )
    result = await db.accounts.delete_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    # Clean up the per-account bot_config override (if any) — the user's
    # default profile is preserved.
    await db.bot_configs.delete_one(
        {"user_id": user["id"], "account_id": account_id}
    )
    return {"ok": True}


@router.post("/{account_id}/rotate-token")
async def rotate_token(account_id: str, user=Depends(get_current_user)):
    db = get_db()
    acc = await db.accounts.find_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    new_token = generate_bridge_token()
    grace_until = (datetime.now(timezone.utc)
                   + timedelta(minutes=15)).isoformat()
    await db.accounts.update_one(
        {"_id": acc["_id"]},
        {"$set": {"bridge_token": new_token,
                  "bridge_token_prev": acc.get("bridge_token"),
                  "bridge_token_prev_expires": grace_until,
                  "status": "disconnected"}},
    )
    return {"bridge_token": new_token, "prev_token_grace_until": grace_until}


@router.post("/{account_id}/request-sync")
async def request_broker_sync(account_id: str, user=Depends(get_current_user)):
    """Queue a deep broker-history sync (7-day lookback) for the EA.

    The EA (v1.39+) picks the request up on its next poll (~5s), re-scans the
    full MT5 deal history for the window and re-pushes every deal. The server
    is idempotent on deal_id but repairs any trade still carrying estimated
    or missing P&L with the exact broker figures.
    """
    db = get_db()
    oid = parse_object_id(account_id)
    acc = await db.accounts.find_one({"_id": oid, "user_id": user["id"]})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    lookback = 7 * 86400
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.accounts.update_one({"_id": oid}, {"$set": {
        "pending_history_sync": {
            "lookback_seconds": lookback,
            "requested_at": now_iso,
            "requested_by": "user",
        },
    }})
    return {"ok": True, "lookback_seconds": lookback, "requested_at": now_iso}


@router.post("/{account_id}/import-positions")
async def import_positions(account_id: str, payload: ImportPositionsRequest,
                            user=Depends(get_current_user)):
    """Manually backfill open positions you can see in MT5 but STOIC doesn't yet
    track — typically positions opened BEFORE the EA was attached.

    Idempotent on `(account_id, mt5_ticket)`: re-running with the same tickets
    is a no-op, so the user can paste from MT5 → Trade tab without worrying
    about duplicates. Once EA v1.25 is installed, the heartbeat-snapshot path
    handles this automatically.
    """
    db = get_db()
    acc = await db.accounts.find_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}
    )
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    now_iso = datetime.now(timezone.utc).isoformat()
    created = 0
    skipped: list[int] = []
    for p in payload.positions:
        exists = await db.trades.find_one({
            "account_id": account_id, "mt5_ticket": int(p.ticket),
        })
        if exists:
            skipped.append(p.ticket)
            continue
        await db.trades.insert_one({
            "user_id": user["id"],
            "account_id": account_id,
            "symbol": p.symbol.upper(),
            "action": p.type,
            "lot_size": p.volume,
            "entry_price": p.price_open,
            "stop_loss": p.sl,
            "take_profit": p.tp,
            "exit_price": None,
            "pnl": 0.0,
            "status": "open",
            "mode": acc.get("mode", "live"),
            "broker": acc.get("broker", "MT5"),
            "mt5_ticket": int(p.ticket),
            "opened_at": now_iso,
            "closed_at": None,
            "origin": "external",
            "external_open": True,
            "manually_imported": True,
        })
        created += 1
    return {
        "created": created,
        "skipped_existing": skipped,
        "total_submitted": len(payload.positions),
    }


@router.get("/{account_id}/bridge-token")
async def get_bridge_token(account_id: str, user=Depends(get_current_user)):
    """Owner-scoped on-demand secret fetch (kept out of list responses)."""
    db = get_db()
    acc = await db.accounts.find_one(
        {"_id": parse_object_id(account_id, "Account"),
         "user_id": user["id"]}, {"bridge_token": 1})
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"bridge_token": acc.get("bridge_token") or ""}


@router.get("/{account_id}/test-connection")
async def test_connection(account_id: str, user=Depends(get_current_user)):
    """Diagnostic snapshot of the EA bridge health for one account.

    Returns connected status + last heartbeat age + current spreads + an
    actionable diagnostic message the Accounts UI can render inline.
    """
    db = get_db()
    acc = await db.accounts.find_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]}
    )
    if not acc:
        raise HTTPException(status_code=404, detail="Account not found")

    is_paper = (acc.get("mode") == "paper")
    last_hb = acc.get("last_heartbeat")
    age_seconds: int | None = None
    if last_hb:
        try:
            ts = datetime.fromisoformat(str(last_hb).replace("Z", "+00:00"))
            age_seconds = int((datetime.now(timezone.utc) - ts).total_seconds())
        except Exception:
            age_seconds = None

    fresh = age_seconds is not None and age_seconds < 60
    connected = bool(is_paper or fresh)

    if is_paper:
        msg = "Paper account — virtual engine always reachable. No EA needed."
        severity = "ok"
    elif age_seconds is None:
        msg = "The EA has never connected. Complete steps 2–5 above to attach EmergentTradingBridge.mq5 to MT5."
        severity = "error"
    elif age_seconds < 60:
        msg = f"EA online — last heartbeat {age_seconds}s ago. Trades will route here."
        severity = "ok"
    elif age_seconds < 600:
        msg = f"EA stale — last heartbeat {age_seconds // 60}min ago. MT5 may have lost focus or internet. Verify AutoTrading is ON."
        severity = "warn"
    else:
        msg = f"EA disconnected — last heartbeat {age_seconds // 60}min ago. Re-attach the EA or restart MT5 (run on a VPS for 24/7 autopilot)."
        severity = "error"

    # Surface broker-account mismatch (EA v1.24+) — when the EA reports a
    # different MT5 login than what STOIC has configured, the diagnostic
    # message MUST flag it loud so the user catches "wrong terminal" or
    # "two EAs attached to one terminal" bugs.
    mismatch = bool(acc.get("broker_account_mismatch"))
    if mismatch and severity == "ok":
        severity = "warn"
        msg = acc.get("broker_account_mismatch_reason") or (
            "EA is reporting from a different MT5 account than this profile expects."
        )

    return {
        "account_id": str(acc["_id"]),
        "mode": acc.get("mode"),
        "connected": connected,
        "fresh": fresh,
        "last_heartbeat": last_hb,
        "age_seconds": age_seconds,
        "balance": acc.get("balance"),
        "equity": acc.get("equity"),
        "current_spreads": acc.get("current_spreads") or {},
        "spreads_updated_at": acc.get("spreads_updated_at"),
        "broker_account_id_reported": acc.get("broker_account_id_reported"),
        "broker_account_mismatch": mismatch,
        "broker_account_mismatch_reason": acc.get("broker_account_mismatch_reason"),
        "diagnostic": {"severity": severity, "message": msg},
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@router.patch("/{account_id}/credentials")
async def update_credentials(account_id: str, payload: AccountCredsUpdate, user=Depends(get_current_user)):
    """Encrypt and store (or clear) broker login passwords on the account.

    Empty string clears a stored credential. None leaves it untouched.
    Live accounts only — paper accounts have no broker credentials.
    """
    db = get_db()
    account = await db.accounts.find_one({"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if (account.get("mode") or "live").lower() == "paper":
        raise HTTPException(status_code=400, detail="Paper accounts do not store broker credentials")

    creds = dict(account.get("creds") or {})
    if payload.investor_password is not None:
        if payload.investor_password == "":
            creds.pop("investor", None)
        else:
            creds["investor"] = vault_encrypt(payload.investor_password)
    if payload.master_password is not None:
        if payload.master_password == "":
            creds.pop("master", None)
        else:
            creds["master"] = vault_encrypt(payload.master_password)

    await db.accounts.update_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]},
        {"$set": {"creds": creds}},
    )
    return {
        "has_investor_password": bool(creds.get("investor")),
        "has_master_password": bool(creds.get("master")),
    }


class RevealRequest(BaseModel):
    password: str = Field(..., min_length=1, description="Account password — re-confirm for credential reveal")
    include_master: bool = Field(False, description="Set true to also reveal master password (highest-risk; use sparingly)")


@router.post("/{account_id}/credentials/reveal")
async def reveal_credentials(account_id: str, payload: RevealRequest,
                             request: Request,
                             user=Depends(get_current_user)):
    """Decrypt and return stored broker passwords for the owner.

    Requires:
      - Authenticated session (cookie)
      - Re-confirmation of the user's account password (defense vs stolen session)
      - include_master must be explicitly set to true to receive master_password
    Every reveal is audit-logged to `credential_reveals` (user, account, IP, ts,
    which secrets returned). Frequent reveal attempts can be rate-limited there.
    """
    db = get_db()
    try:
        acct_oid = ObjectId(account_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Account not found")
    from security import rate_limit
    await rate_limit(db, "cred_reveal", user["id"], 3, 3600,
                     "Too many credential reveals. Try again later.",
                     request=request)
    account = await db.accounts.find_one({"_id": acct_oid, "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    # Re-confirm session password — prevents reveal via stolen/forgotten session
    fresh_user = await db.users.find_one({"_id": ObjectId(user["id"])})
    pwhash = (fresh_user or {}).get("password_hash") if fresh_user else None
    if not pwhash or not verify_password(payload.password, pwhash):
        # Audit failed attempts so brute-force shows up
        await db.credential_reveals.insert_one({
            "user_id": user["id"], "account_id": account_id,
            "result": "wrong_password", "revealed": [],
            "at": datetime.now(timezone.utc).isoformat(),
        })
        raise HTTPException(status_code=403, detail="Password re-confirmation failed")

    creds = account.get("creds") or {}
    out = {"investor_password": None}
    if payload.include_master:
        out["master_password"] = None
    revealed_keys: list[str] = []
    try:
        if creds.get("investor"):
            out["investor_password"] = vault_decrypt(creds["investor"])
            revealed_keys.append("investor")
        if payload.include_master and creds.get("master"):
            out["master_password"] = vault_decrypt(creds["master"])
            revealed_keys.append("master")
    except Exception:
        raise HTTPException(status_code=500, detail="Could not decrypt stored credentials")

    # Audit log — never includes the plaintext
    await db.credential_reveals.insert_one({
        "user_id": user["id"], "account_id": account_id,
        "result": "ok", "revealed": revealed_keys,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    return out


@router.post("/{account_id}/unblock")
async def unblock_account_route(account_id: str, user=Depends(get_current_user)):
    """Clear the trading_blocked flag set by the broker-rejection circuit
    breaker (iter-71). Use after fixing the underlying issue (recompiling
    EA v1.29+, fixing broker symbol config, etc.)."""
    db = get_db()
    acct_oid = parse_object_id(account_id, "Account")
    account = await db.accounts.find_one({"_id": acct_oid, "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if not account.get("trading_blocked"):
        return {"ok": True, "was_blocked": False,
                "message": "Account was not blocked"}
    from broker_reject_breaker import unblock_account
    ok = await unblock_account(db, acct_oid)
    return {"ok": ok, "was_blocked": True,
            "message": f"Trading re-enabled on {account.get('label')}"}


@router.get("/{account_id}/block-status")
async def block_status(account_id: str, user=Depends(get_current_user)):
    """Read-only check — surfaces the broker-rejection breaker state for
    the dashboard tile."""
    db = get_db()
    acct_oid = parse_object_id(account_id, "Account")
    account = await db.accounts.find_one({"_id": acct_oid, "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    return {
        "account_id": account_id,
        "label": account.get("label"),
        "trading_blocked": bool(account.get("trading_blocked")),
        "block_reason": account.get("block_reason"),
        "block_retcode": account.get("block_retcode"),
        "block_retcode_label": account.get("block_retcode_label"),
        "block_hint": account.get("block_hint"),
        "blocked_at": account.get("blocked_at"),
    }


@router.put("/{account_id}/symbol-suffix")
async def set_symbol_suffix(account_id: str, payload: dict,
                            user=Depends(get_current_user)):
    """Set/clear a per-account symbol_suffix (iter-71b).

    Brokers like VT Markets rename instruments — `XAUUSD` becomes
    `XAUUSD.x` / `XAUUSDpro` / etc. The bot appends this suffix to every
    trade symbol BEFORE sending to the EA, sidestepping the need to
    recompile the MT5 binary.

    Auto-unblocks the account if it was previously halted by the
    broker-rejection circuit breaker — the suffix change is the user's
    signal that they've fixed the underlying issue.

    Body: {"symbol_suffix": ".x"}  or  {"symbol_suffix": ""}  to clear.
    """
    db = get_db()
    acct_oid = parse_object_id(account_id, "Account")
    account = await db.accounts.find_one({"_id": acct_oid, "user_id": user["id"]})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")

    raw = (payload.get("symbol_suffix") or "").strip()
    # Light validation — suffix must be short, ASCII-safe, no spaces.
    if len(raw) > 12:
        raise HTTPException(status_code=400,
                            detail="symbol_suffix too long (max 12 chars)")
    if any(c.isspace() for c in raw):
        raise HTTPException(status_code=400,
                            detail="symbol_suffix may not contain whitespace")

    updates = {"symbol_suffix": raw}
    was_blocked = bool(account.get("trading_blocked"))
    if was_blocked:
        updates["trading_blocked"] = False
        updates["unblocked_at"] = datetime.now(timezone.utc).isoformat()

    unset = {}
    if was_blocked:
        unset = {"block_reason": "", "block_retcode": "",
                 "block_retcode_label": "", "block_hint": "",
                 "block_failure_count": ""}

    update_op = {"$set": updates}
    if unset:
        update_op["$unset"] = unset
    await db.accounts.update_one({"_id": acct_oid}, update_op)

    return {
        "ok": True,
        "account_id": account_id,
        "symbol_suffix": raw or None,
        "auto_unblocked": was_blocked,
        "message": (f"Suffix '{raw}' set" if raw else "Suffix cleared")
                   + (" + trading re-enabled" if was_blocked else ""),
    }


# ─────────────────────────────────────────────────────────────────────────
# iter-86 · Test trade — proves end-to-end execution per account
# ─────────────────────────────────────────────────────────────────────────
@router.post("/{account_id}/test-trade")
async def fire_test_trade(account_id: str, user=Depends(get_current_user)):
    """Fire a tiny 0.01-lot BUY with a tight TP to validate the full execution
    pipe on demand — no need to wait for the AI to choose BUY/SELL.

    What it does:
      · BUY 0.01 lots of XAUUSD (or BTCUSD if account is on OnEquity / a
        broker without gold in MarketWatch).
      · TP set ~3 USD above the live ask (closes in seconds when price ticks
        up); SL set ~30 USD below (wide buffer — we don't actually want this
        to take a loss, just to validate routing).
      · Bypasses the AI signal generator entirely. Still goes through the
        iter-82 per-base resolver, safety guardian, and EA bridge — so a
        green test trade confirms EVERYTHING in the live pipeline works.
      · Tagged `origin="test_trade"` + `is_test=true` so it's excluded from
        win-rate, PnL and analytics dashboards.

    Refuses to fire if:
      · Account not owned by caller.
      · Paper account (use the bot directly — no execution pipe to validate).
      · Heartbeat is stale > 120s (the EA needs to be alive to pick it up).
      · The base symbol isn't offered by the broker (per-base resolver
        returns None).
    """
    db = get_db()
    oid = parse_object_id(account_id, "Account")
    account = await db.accounts.find_one({"_id": oid})
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    if account.get("user_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Account not yours")
    if account.get("mode") == "paper":
        raise HTTPException(
            status_code=400,
            detail={"code": "paper_account",
                    "message": "Test trades are for validating the MT5 execution "
                               "pipeline — paper accounts have nothing to test."},
        )

    # Heartbeat freshness gate (≤2 min)
    hb = account.get("last_heartbeat")
    # iter-158 review P0-5 — FAIL CLOSED: no heartbeat ever, or an
    # unparseable heartbeat, must block exactly like a stale one.
    if not hb:
        raise HTTPException(
            status_code=409,
            detail={"code": "ea_never_connected",
                    "message": "This account's EA has never sent a heartbeat. "
                               "Connect the terminal before firing a test trade."},
        )
    try:
        hb_dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
        age_s = (datetime.now(timezone.utc) - hb_dt).total_seconds()
    except HTTPException:
        raise
    except Exception:
        age_s = None
    if age_s is None or age_s > 120:
        raise HTTPException(
            status_code=409,
            detail={"code": "stale_ea",
                    "message": (f"EA hasn't checked in for {int(age_s)}s. "
                                if age_s is not None else
                                "EA heartbeat timestamp is unreadable. ")
                    + "Open MT5 + ensure AutoTrading is ON, then retry."},
        )

    # Pick a base symbol the broker actually offers AND whose market is
    # currently open (weekends: XAUUSD/EURUSD closed → falls to BTCUSD).
    from broker_symbol_detector import resolve_broker_symbol
    from microstructure import is_market_closed
    available = account.get("available_symbols")
    chosen_base = None
    for candidate in ("XAUUSD", "BTCUSD", "EURUSD"):
        if is_market_closed(candidate):
            continue
        if resolve_broker_symbol(candidate, available, account.get("auto_detected_symbol_suffix") or ""):
            chosen_base = candidate
            break
    if chosen_base is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "no_tradeable_symbol",
                    "message": "No open-market symbol available right now — broker's MarketWatch must list XAUUSD, BTCUSD, or EURUSD (BTCUSD trades 24/7). Add one in MT5 (right-click MarketWatch → Show All)."},
        )

    # Fetch a live price so we can stamp entry/SL/TP on the signal — the
    # safety guardian refuses any signal missing risk inputs (lot/entry/SL).
    # Falls back to a sane default if the price feed is unavailable so test
    # trades still validate the routing path.
    from market import get_quote
    _default_px = {"XAUUSD": 2400.0, "BTCUSD": 60000.0, "EURUSD": 1.10}
    try:
        quote = await get_quote(chosen_base)
        price = float((quote or {}).get("price") or 0) or _default_px[chosen_base]
    except Exception:
        price = _default_px[chosen_base]

    # pip size + lever for SL/TP distance per asset family
    if chosen_base == "XAUUSD":
        sl_dist, tp_dist = 30.0, 3.0           # USD per oz
    elif chosen_base == "BTCUSD":
        sl_dist, tp_dist = 500.0, 50.0         # USD per BTC
    else:  # EURUSD
        sl_dist, tp_dist = 0.0050, 0.0005      # 50 pips SL, 5 pips TP

    entry = price
    stop_loss = round(entry - sl_dist, 5)
    take_profit = round(entry + tp_dist, 5)

    # Compose a tiny BUY signal. TP/SL are deliberately small/wide so the
    # trade closes quickly via TP in normal liquid markets but never takes
    # a meaningful loss if it sits open.
    signal = {
        "symbol": chosen_base,
        "action": "BUY",
        "lot_size": 0.01,
        "confidence": 99,
        "entry_price": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "tp_pips": 30 if chosen_base == "XAUUSD" else 50,
        "sl_pips": 300 if chosen_base == "XAUUSD" else 500,
        "origin": "test_trade",
        "is_test": True,
        "test_initiated_by": user.get("email"),
        "reason": "Manual test trade — validates execution pipeline (iter-86).",
    }

    from execution import for_account as engine_for_account
    engine = engine_for_account(account)
    try:
        result = await engine.execute(
            user_id=user["id"], account=account, signal=signal,
            max_concurrent=999,           # bypass concurrent cap for test
            cfg_account_id=account_id,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail={"code": "execution_error",
                    "message": "Order execution failed — see server logs."},
        )

    if "blocked" in result:
        raise HTTPException(
            status_code=409,
            detail={"code": result["blocked"],
                    "message": f"Test trade refused at execution layer: {result['blocked']}",
                    "context": result},
        )

    # Mark the persisted trade as a test so analytics ignore it.
    if result.get("id"):
        await db.trades.update_one(
            {"_id": ObjectId(result["id"])},
            {"$set": {"is_test": True, "origin": "test_trade"}},
        )

    return {
        "ok": True,
        "trade_id": result.get("id"),
        "symbol": result.get("symbol"),
        "lot_size": signal["lot_size"],
        "message": (
            f"Test trade queued: BUY 0.01 {result.get('symbol')}. "
            "The EA will pick it up on the next poll (~10s). "
            "Watch the Trades page to see fill + close — it should close via TP within seconds in liquid markets."
        ),
    }


@router.get("/certification")
async def accounts_certification(user=Depends(get_current_user)):
    """Iter-151 · per-account go-live certification checklist, computed from
    the EA's own reported state (heartbeats, specs, clock, history sync)."""
    from routes.diagnostic_routes import LATEST_EA
    db = get_db()
    now = datetime.now(timezone.utc)

    def _age(ts):
        try:
            d = datetime.fromisoformat(str(ts))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return (now - d).total_seconds()
        except Exception:
            return None

    out = []
    async for a in db.accounts.find({"user_id": user["id"],
                                     "status": {"$ne": "deleted"}}):
        hb_age = _age(a.get("last_heartbeat"))
        specs = a.get("symbol_specs") or {}
        specs_age = _age(a.get("symbol_specs_updated_at"))
        spreads_age = _age(a.get("spreads_updated_at"))
        sync_age = _age(a.get("last_full_sync_at"))
        v = a.get("ea_version")
        acct_type = str(a.get("account_type") or "").lower()
        checks = [
            {"key": "ea_version", "label": "EA version",
             "value": v or "—", "ok": bool(v) and v == LATEST_EA,
             "hint": f"latest is {LATEST_EA}"},
            {"key": "bridge_paired", "label": "Bridge paired",
             "value": "yes" if a.get("bridge_token") else "no",
             "ok": bool(a.get("bridge_token"))},
            {"key": "heartbeat", "label": "EA heartbeat",
             "value": f"{int(hb_age)}s ago" if hb_age is not None else "never",
             "ok": hb_age is not None and hb_age < 300},
            {"key": "account_type", "label": "Account type",
             "value": acct_type or "unknown",
             "ok": acct_type in ("hedging", "netting")},
            {"key": "symbol_specs", "label": "Symbol / stop-level specs",
             "value": f"{len(specs)} symbols",
             "ok": len(specs) > 0 and specs_age is not None
                   and specs_age < 86400},
            {"key": "spread_feed", "label": "Tick / spread feed",
             "value": (f"{int(spreads_age)}s ago"
                       if spreads_age is not None else "never"),
             "ok": spreads_age is not None and spreads_age < 600},
            {"key": "clock_sync", "label": "Broker clock offset",
             "value": (f"{a.get('broker_utc_offset_sec')}s"
                       if a.get("broker_utc_offset_sec") is not None
                       else "unknown"),
             "ok": a.get("broker_utc_offset_sec") is not None},
            {"key": "history_sync", "label": "History synchronization",
             "value": (f"{int(sync_age / 3600)}h ago"
                       if sync_age is not None else "never"),
             "ok": sync_age is not None and sync_age < 86400},
            {"key": "identity", "label": "Terminal identity",
             "value": ("mismatch" if a.get("broker_account_mismatch")
                       else "verified"),
             "ok": not a.get("broker_account_mismatch")},
            {"key": "stop_freeze_levels", "label": "Stop/freeze levels",
             "value": (f"{sum(1 for s in specs.values() if s.get('stops_level_points') is not None)}"
                       f"/{len(specs)} symbols"),
             "ok": len(specs) > 0 and all(
                 s.get("stops_level_points") is not None
                 for s in specs.values())},
            # iter-154 — round 3 additions: OrderCheck support, fill policy,
            # symbol mapping coverage.
            {"key": "order_check", "label": "OrderCheck preflight",
             "value": (f"supported (EA v{v})" if v and str(v) >= "1.50"
                       else "requires EA v1.50+"),
             "ok": bool(v) and str(v) >= "1.50",
             "hint": "broker-native OrderCheck rejection before submit"},
            {"key": "fill_policy", "label": "Fill policy",
             "value": ("EA auto-select per symbol" if v and str(v) >= "1.28"
                       else "unknown"),
             "ok": bool(v) and str(v) >= "1.28",
             "hint": "EA v1.28+ picks the broker-supported filling mode"},
            {"key": "symbol_mapping", "label": "Symbol mapping",
             "value": f"{len(a.get('available_symbols') or [])} symbols discovered",
             "ok": len(a.get("available_symbols") or []) > 0,
             "hint": "EA reports the broker's tradable symbol names"},
        ]
        cert_age = _age(a.get("demo_certified_at"))
        base_pass = all(c["ok"] for c in checks)
        checks.append(
            {"key": "demo_certified", "label": "Demo certification",
             "value": (f"{int(cert_age / 86400)}d ago"
                       if cert_age is not None else "never"),
             "ok": cert_age is not None and cert_age < 30 * 86400,
             "hint": "stamped via POST /accounts/{id}/certify once all "
                     "other checks pass"})
        passed = sum(1 for c in checks if c["ok"])
        out.append({"account_id": str(a["_id"]), "label": a.get("label"),
                    "mode": a.get("mode"),
                    "checks": checks, "passed": passed,
                    "total": len(checks),
                    "can_certify": base_pass,
                    "certified": passed == len(checks)})
    return {"items": out, "generated_at": now.isoformat()}


@router.post("/{account_id}/certify")
async def certify_account(account_id: str,
                          user=Depends(get_current_user)):
    """Iter-152 · stamp demo certification — only allowed once every other
    go-live check currently passes (server-side re-verification)."""
    cert = await accounts_certification(user=user)
    item = next((i for i in cert["items"]
                 if i["account_id"] == account_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Account not found")
    if not item.get("can_certify"):
        failing = [c["key"] for c in item["checks"]
                   if not c["ok"] and c["key"] != "demo_certified"]
        raise HTTPException(status_code=400, detail={
            "code": "certification_blocked",
            "message": "Fix the failing checks before certifying.",
            "failing": failing})
    db = get_db()
    stamp = datetime.now(timezone.utc).isoformat()
    await db.accounts.update_one(
        {"_id": ObjectId(account_id), "user_id": user["id"]},
        {"$set": {"demo_certified_at": stamp,
                  "demo_certified_by": user.get("email")}})
    return {"ok": True, "demo_certified_at": stamp}
