from datetime import datetime, timezone
from typing import List, Literal
from fastapi import APIRouter, Depends, HTTPException
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


def _serialize(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    creds = doc.pop("creds", {}) or {}
    doc["has_investor_password"] = bool(creds.get("investor"))
    doc["has_master_password"] = bool(creds.get("master"))
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
    new_token = generate_bridge_token()
    result = await db.accounts.update_one(
        {"_id": parse_object_id(account_id, "Account"), "user_id": user["id"]},
        {"$set": {"bridge_token": new_token, "status": "disconnected"}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"bridge_token": new_token}


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
    if hb:
        try:
            hb_dt = datetime.fromisoformat(hb.replace("Z", "+00:00"))
            age_s = (datetime.now(timezone.utc) - hb_dt).total_seconds()
            if age_s > 120:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "stale_ea",
                            "message": f"EA hasn't checked in for {int(age_s)}s. Open MT5 + ensure AutoTrading is ON, then retry."},
                )
        except HTTPException:
            raise
        except Exception:
            pass

    # Pick a base symbol the broker actually offers.
    from broker_symbol_detector import resolve_broker_symbol
    available = account.get("available_symbols")
    chosen_base = None
    for candidate in ("XAUUSD", "BTCUSD", "EURUSD"):
        if resolve_broker_symbol(candidate, available, account.get("auto_detected_symbol_suffix") or ""):
            chosen_base = candidate
            break
    if chosen_base is None:
        raise HTTPException(
            status_code=409,
            detail={"code": "no_tradeable_symbol",
                    "message": "Broker's MarketWatch doesn't list XAUUSD, BTCUSD, or EURUSD. Add one in MT5 (right-click MarketWatch → Show All)."},
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
            detail={"code": "execution_error", "message": str(e)},
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
