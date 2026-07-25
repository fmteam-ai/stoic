"""iter-139 · Phase H-C — Enterprise API layer.

Two routers:
  mgmt_router  (/api-keys, cookie/JWT session auth) — create / list / revoke keys
  public_router (/v1, X-API-Key auth)               — read-only programmatic access

Key model (irretrievable): full key `stoic_live_<43 urlsafe chars>` shown ONCE at
creation; DB stores only sha256 hash + display prefix. Scopes: read:accounts,
read:trades, read:portfolio. Simple per-key in-memory sliding-window rate limit.
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from step_up import require_step_up, audit_event
from entitlements import require_feature
from pydantic import BaseModel, Field

from auth import get_current_user
from database import get_db
from route_utils import parse_object_id

mgmt_router = APIRouter(prefix="/api-keys", tags=["enterprise-api-keys"],
                        dependencies=[Depends(require_feature("api_access"))])
public_router = APIRouter(prefix="/v1", tags=["enterprise-public-api"])

KEY_PREFIX = "stoic_live_"
PREFIX_LEN = len(KEY_PREFIX) + 8          # display/lookup prefix: stoic_live_XXXXXXXX
VALID_SCOPES = {"read:accounts", "read:trades", "read:portfolio"}

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# In-memory sliding window: {key_id: [timestamps]} — best-effort, single process.
_rate_windows: dict = {}


def _hash_key(full_key: str) -> str:
    return hashlib.sha256(full_key.encode("utf-8")).hexdigest()


def _generate_key():
    full_key = f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"
    return full_key, full_key[:PREFIX_LEN], _hash_key(full_key)


def _serialize_key(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "name": doc.get("name"),
        "key_prefix": doc.get("key_prefix"),
        "scopes": doc.get("scopes") or [],
        "rate_limit_per_minute": doc.get("rate_limit_per_minute"),
        "revoked_at": doc.get("revoked_at"),
        "last_used_at": doc.get("last_used_at"),
        "total_requests": doc.get("total_requests") or 0,
        "created_at": doc.get("created_at"),
    }


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    scopes: List[str] = Field(..., min_length=1)
    rate_limit_per_minute: int = Field(default=120, ge=1, le=10000)


# ---------------------------------------------------------------- management

@mgmt_router.get("")
async def list_api_keys(user=Depends(get_current_user)):
    db = get_db()
    docs = await db.api_keys.find({"user_id": user["id"]}).sort("created_at", -1).to_list(length=100)
    return [_serialize_key(d) for d in docs]


@mgmt_router.post("")
async def create_api_key(payload: ApiKeyCreate, request: Request,
                         user=Depends(get_current_user)):
    bad = [s for s in payload.scopes if s not in VALID_SCOPES]
    if bad:
        raise HTTPException(status_code=400,
                            detail=f"Unknown scopes: {bad}. Valid: {sorted(VALID_SCOPES)}")
    db = get_db()
    # Step-up MFA — API keys grant programmatic account access.
    await require_step_up(db, user, request, "api_key_create")
    active = await db.api_keys.count_documents({"user_id": user["id"], "revoked_at": None})
    if active >= 10:
        raise HTTPException(status_code=400,
                            detail="Key limit reached (10 active). Revoke unused keys first.")
    full_key, key_prefix, key_hash = _generate_key()
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "user_id": user["id"],
        "name": payload.name.strip(),
        "key_prefix": key_prefix,
        "key_hash": key_hash,
        "scopes": sorted(set(payload.scopes)),
        "rate_limit_per_minute": payload.rate_limit_per_minute,
        "revoked_at": None,
        "last_used_at": None,
        "total_requests": 0,
        "created_at": now,
    }
    res = await db.api_keys.insert_one(doc)
    doc["_id"] = res.inserted_id
    await audit_event(db, user["id"], "api_key_created",
                      {"name": doc["name"], "key_prefix": key_prefix,
                       "scopes": doc["scopes"]}, request, step_up=True)
    # Full key returned ONCE — never persisted, never logged.
    return {"api_key": full_key, "record": _serialize_key(doc)}


@mgmt_router.post("/{key_id}/revoke")
async def revoke_api_key(key_id: str, user=Depends(get_current_user)):
    db = get_db()
    res = await db.api_keys.update_one(
        {"_id": parse_object_id(key_id, "API key"), "user_id": user["id"], "revoked_at": None},
        {"$set": {"revoked_at": datetime.now(timezone.utc).isoformat()}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="API key not found or already revoked")
    return {"ok": True}


# ---------------------------------------------------------------- key auth

async def authenticate_api_key(x_api_key: Optional[str] = Security(api_key_header)) -> dict:
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    db = get_db()
    doc = await db.api_keys.find_one({"key_prefix": x_api_key[:PREFIX_LEN], "revoked_at": None})
    if not doc or not hmac.compare_digest(_hash_key(x_api_key), doc["key_hash"]):
        raise HTTPException(status_code=401, detail="Invalid API key")

    # iter-122 Phase 2 — re-verify the OWNER's plan still includes API access
    # at request time (a downgrade revokes programmatic access immediately).
    from subscription_service import get_user_tier
    from subscription_plans import get_tier_features
    owner_tier = await get_user_tier(doc["user_id"])
    if owner_tier != "admin" and not get_tier_features(owner_tier).api_access:
        raise HTTPException(
            status_code=402,
            detail={"error": "feature_locked", "feature": "api_access",
                    "message": "API access requires the Professional plan or higher."})

    # Sliding-window rate limit (per key, per process, 60s window)
    now = datetime.now(timezone.utc)
    kid = str(doc["_id"])
    window = [t for t in _rate_windows.get(kid, []) if (now - t).total_seconds() < 60]
    limit = doc.get("rate_limit_per_minute") or 120
    if len(window) >= limit:
        raise HTTPException(status_code=429, detail="Rate limit exceeded for this API key")
    window.append(now)
    _rate_windows[kid] = window

    await db.api_keys.update_one(
        {"_id": doc["_id"]},
        {"$set": {"last_used_at": now.isoformat()}, "$inc": {"total_requests": 1}},
    )
    return doc


def require_scope(scope: str):
    async def dep(key=Depends(authenticate_api_key)) -> dict:
        if scope not in (key.get("scopes") or []):
            raise HTTPException(status_code=403,
                                detail=f"API key missing required scope: {scope}")
        return key
    return dep


# ---------------------------------------------------------------- public v1

@public_router.get("/me")
async def v1_me(key=Depends(authenticate_api_key)):
    """Key introspection — verify connectivity and inspect granted scopes."""
    return {
        "key_prefix": key["key_prefix"],
        "name": key.get("name"),
        "scopes": key.get("scopes") or [],
        "rate_limit_per_minute": key.get("rate_limit_per_minute"),
    }


@public_router.get("/accounts")
async def v1_accounts(key=Depends(require_scope("read:accounts"))):
    """Read-only account list. Secrets (bridge tokens, credentials) are never exposed."""
    db = get_db()
    docs = await db.accounts.find({"user_id": key["user_id"]}).sort("created_at", -1).to_list(length=100)
    return {"accounts": [{
        "id": str(a["_id"]),
        "label": a.get("label"),
        "broker": a.get("broker"),
        "server": a.get("server"),
        "account_number": a.get("account_number"),
        "account_type": a.get("account_type"),
        "base_currency": a.get("base_currency"),
        "mode": a.get("mode"),
        "group": a.get("group"),
        "trading_enabled": a.get("trading_enabled") is not False,
        "balance": a.get("balance"),
        "equity": a.get("equity"),
        "open_positions": a.get("open_positions") or 0,
        "last_heartbeat": a.get("last_heartbeat"),
        "created_at": a.get("created_at"),
    } for a in docs]}


@public_router.get("/trades")
async def v1_trades(
    status: Optional[str] = None,
    symbol: Optional[str] = None,
    account_id: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    key=Depends(require_scope("read:trades")),
):
    """Trade history with filters + pagination (max 500 per page)."""
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    q: dict = {"user_id": key["user_id"]}
    if status:
        q["status"] = status
    if symbol:
        q["$or"] = [{"symbol": symbol.upper()}, {"base_symbol": symbol.upper()}]
    if account_id:
        q["account_id"] = account_id
    if from_date or to_date:
        rng = {}
        if from_date:
            rng["$gte"] = from_date
        if to_date:
            rng["$lte"] = to_date
        q["closed_at"] = rng
    db = get_db()
    total = await db.trades.count_documents(q)
    docs = await db.trades.find(q).sort("opened_at", -1).skip(offset).to_list(length=limit)
    return {"total": total, "limit": limit, "offset": offset, "trades": [{
        "id": str(t["_id"]),
        "account_id": t.get("account_id"),
        "symbol": t.get("symbol"),
        "base_symbol": t.get("base_symbol"),
        "action": t.get("action"),
        "lot_size": t.get("lot_size"),
        "entry_price": t.get("entry_price"),
        "exit_price": t.get("exit_price"),
        "stop_loss": t.get("stop_loss"),
        "take_profit": t.get("take_profit"),
        "pnl": t.get("pnl"),
        "status": t.get("status"),
        "origin": t.get("origin"),
        "scope": t.get("scope"),
        "close_reason": t.get("close_reason"),
        "opened_at": t.get("opened_at"),
        "closed_at": t.get("closed_at"),
        "broker": t.get("broker"),
        "mode": t.get("mode"),
    } for t in docs]}


@public_router.get("/portfolio")
async def v1_portfolio(key=Depends(require_scope("read:portfolio"))):
    """Aggregate portfolio snapshot — same math as the web Accounts overview."""
    from routes.account_routes import accounts_overview
    return await accounts_overview(user={"id": key["user_id"]})
