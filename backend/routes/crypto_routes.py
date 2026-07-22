"""Crypto (Binance Spot via CCXT) REST routes — /api/crypto/*.

Scope (v1):
  • POST   /api/crypto/accounts      → add a Binance Spot account (keys encrypted at rest)
  • GET    /api/crypto/accounts      → list user's crypto accounts (creds masked)
  • DELETE /api/crypto/accounts/{id} → remove
  • GET    /api/crypto/accounts/{id}/balance       → live USDT balance
  • GET    /api/crypto/accounts/{id}/ticker?symbol → live mid
  • POST   /api/crypto/accounts/{id}/execute       → manually fire a signal
                                                     (admin/dev — routes through
                                                      the full safety pipeline)
  • POST   /api/crypto/accounts/{id}/verify        → no-op fetch_balance to test creds
"""
from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from bson import ObjectId

from auth import get_current_user, generate_bridge_token
from database import get_db
from secrets_vault import encrypt as vault_encrypt, mask as vault_mask
from crypto_bridge.ccxt_engine import (
    CCXTClient, normalize_symbol, _live_enabled,
    EXCHANGES, SUPPORTED_EXCHANGES, DEFAULT_EXCHANGE_ID,
    check_reachability,
)
from crypto_bridge.binance_engine import BinanceCCXTEngine

router = APIRouter(prefix="/crypto", tags=["crypto"])
logger = logging.getLogger("crypto.routes")


# ============================== MODELS ==============================
class BinanceAccountCreate(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    api_key: str = Field(min_length=10)
    api_secret: str = Field(min_length=10)
    testnet: bool = True  # default-safe: testnet unless explicitly flipped
    initial_balance: float = 10000.0  # bookkeeping starting value (testnet has dummy funds)
    exchange_id: str = Field(default=DEFAULT_EXCHANGE_ID)  # binance / binanceus / kraken / okx / kucoin
    api_passphrase: Optional[str] = None  # required for OKX/KuCoin only


class CryptoSignal(BaseModel):
    symbol: str = "BTCUSD"                  # internal symbol; mapped to BTC/USDT
    action: Literal["BUY", "SELL"]
    lot_size: float = Field(gt=0)           # base-asset amount (e.g. 0.001 BTC)
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    execution_hint: Optional[Literal["market", "limit"]] = "market"
    limit_price: Optional[float] = None
    origin: str = "manual"


# ============================== HELPERS ==============================
def _serialize(doc: dict) -> dict:
    out = dict(doc)
    out["id"] = str(out.pop("_id"))
    creds = out.pop("creds", {}) or {}
    # Never echo the encrypted blob — just confirm presence.
    out["has_credentials"] = bool(creds.get("api_key") and creds.get("api_secret"))
    out["has_passphrase"] = bool(creds.get("api_passphrase"))
    # Default exchange_id to "binance" for legacy docs written before
    # multi-exchange support landed.
    out.setdefault("exchange_id", DEFAULT_EXCHANGE_ID)
    out["exchange_label"] = EXCHANGES.get(out["exchange_id"], {}).get(
        "label", out["exchange_id"]
    )
    # Mask the key fingerprint (decrypt+mask) so the UI can show "•••• ABCD"
    # without exposing the secret.
    try:
        if creds.get("api_key"):
            from secrets_vault import decrypt as vault_decrypt
            out["api_key_masked"] = vault_mask(vault_decrypt(creds["api_key"]), keep=4)
    except Exception:
        out["api_key_masked"] = "•••• ????"
    return out


async def _load_user_account(db, user, account_id: str) -> dict:
    try:
        oid = ObjectId(account_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Bad account id")
    acc = await db.accounts.find_one({"_id": oid, "user_id": user["id"], "kind": "binance"})
    if not acc:
        raise HTTPException(status_code=404, detail="Binance account not found")
    return acc


# ============================== ROUTES ==============================
@router.get("/accounts")
async def list_crypto_accounts(user=Depends(get_current_user)):
    db = get_db()
    cursor = db.accounts.find({"user_id": user["id"], "kind": "binance"}).sort("created_at", -1)
    docs = await cursor.to_list(length=50)
    return [_serialize(d) for d in docs]


@router.post("/accounts")
async def create_crypto_account(payload: BinanceAccountCreate, user=Depends(get_current_user)):
    db = get_db()

    # Validate exchange_id up front
    exchange_id = (payload.exchange_id or DEFAULT_EXCHANGE_ID).lower()
    meta = EXCHANGES.get(exchange_id)
    if not meta:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported exchange. Choose one of: {', '.join(SUPPORTED_EXCHANGES)}",
        )
    if meta["passphrase"] and not (payload.api_passphrase and payload.api_passphrase.strip()):
        raise HTTPException(
            status_code=422,
            detail=f"{meta['label']} requires an API passphrase. Paste it into the Passphrase field.",
        )

    # Encrypt creds at rest.
    enc_key = vault_encrypt(payload.api_key.strip())
    enc_sec = vault_encrypt(payload.api_secret.strip())
    enc_pass = vault_encrypt(payload.api_passphrase.strip()) if payload.api_passphrase else None

    # Sanity probe: do a fetch_balance immediately to verify keys before persist.
    probe_account = {
        "creds": {"api_key": enc_key, "api_secret": enc_sec, "api_passphrase": enc_pass},
        "testnet": payload.testnet,
        "live": not payload.testnet,
        "exchange_id": exchange_id,
    }
    try:
        async with CCXTClient(probe_account) as client:
            await client.fetch_balance()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=422,
            detail={"code": "exchange_key_verify_failed",
                    "message": f"Could not verify keys against {meta['label']}."},
        )

    doc = {
        "user_id": user["id"],
        "kind": "binance",                          # legacy "kind" field reused as the crypto-account marker
        "exchange_id": exchange_id,
        "label": payload.label,
        "broker": meta["label"].upper().replace(" ", "_") + "_SPOT",
        "server": f"{exchange_id}-{'testnet' if payload.testnet else 'live'}",
        "account_number": "—",            # n/a for ccxt
        "account_type": "standard",       # crypto has no microcent convention
        "base_currency": meta.get("default_quote", "USDT"),
        "mode": "paper" if payload.testnet else "live",
        "testnet": payload.testnet,
        "live": not payload.testnet,
        "balance": float(payload.initial_balance),
        "equity": float(payload.initial_balance),
        "free_margin": float(payload.initial_balance),
        "creds": {"api_key": enc_key, "api_secret": enc_sec, "api_passphrase": enc_pass},
        "bridge_token": generate_bridge_token(),  # reused as a generic account-secret
        "status": "connected",
        "last_heartbeat": datetime.now(timezone.utc).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    r = await db.accounts.insert_one(doc)
    doc["_id"] = r.inserted_id
    return _serialize(doc)


@router.delete("/accounts/{account_id}")
async def delete_crypto_account(account_id: str, user=Depends(get_current_user)):
    db = get_db()
    acc = await _load_user_account(db, user, account_id)
    await db.accounts.delete_one({"_id": acc["_id"]})
    return {"ok": True, "deleted_id": str(acc["_id"])}


@router.get("/accounts/{account_id}/balance")
async def crypto_balance(account_id: str, user=Depends(get_current_user)):
    db = get_db()
    acc = await _load_user_account(db, user, account_id)
    try:
        async with CCXTClient(acc) as client:
            bal = await client.fetch_balance()
    except Exception as e:  # noqa: BLE001
        logger.warning("exchange balance error: %s", e)
        raise HTTPException(status_code=502, detail={
            "code": "exchange_error",
            "message": "Exchange unavailable — try again shortly."})
    total = bal.get("total") or {}
    free = bal.get("free") or {}
    used = bal.get("used") or {}
    # Trim to interesting assets only.
    interesting = {a for a in list(total.keys())
                   if (total.get(a) or 0) > 0 or a in {"USDT", "BTC", "ETH", "SOL"}}
    return {
        "testnet": acc.get("testnet", True),
        "assets": [
            {"asset": a, "free": float(free.get(a) or 0),
             "used": float(used.get(a) or 0),
             "total": float(total.get(a) or 0)}
            for a in sorted(interesting)
        ],
    }


@router.get("/accounts/{account_id}/ticker")
async def crypto_ticker(account_id: str, symbol: str = "BTC/USDT",
                       user=Depends(get_current_user)):
    db = get_db()
    acc = await _load_user_account(db, user, account_id)
    ccxt_sym = normalize_symbol(symbol, acc.get("exchange_id", DEFAULT_EXCHANGE_ID))
    try:
        async with CCXTClient(acc) as client:
            t = await client.fetch_ticker(ccxt_sym)
    except Exception as e:  # noqa: BLE001
        logger.warning("exchange ticker error: %s", e)
        raise HTTPException(status_code=502, detail={
            "code": "exchange_error",
            "message": "Exchange unavailable — try again shortly."})
    return {
        "symbol": ccxt_sym,
        "bid": t.get("bid"),
        "ask": t.get("ask"),
        "last": t.get("last"),
        "high": t.get("high"),
        "low": t.get("low"),
        "ts": t.get("timestamp"),
    }


@router.post("/accounts/{account_id}/execute")
async def crypto_execute(account_id: str, payload: CryptoSignal,
                        user=Depends(get_current_user)):
    """Manually fire a signal through the full safety pipeline.

    Useful for: (a) integration verification, (b) UI panic buttons,
    (c) one-off manual entries that should still respect the Guardian.
    """
    db = get_db()
    acc = await _load_user_account(db, user, account_id)
    # Master kill switch — even if account.live is true, only fire live if
    # the global env flag is on AND the account flipped out of testnet.
    if not acc.get("testnet") and not _live_enabled():
        raise HTTPException(
            status_code=403,
            detail="Live crypto execution disabled (BINANCE_LIVE_ENABLED=false)",
        )

    engine = BinanceCCXTEngine()
    signal = payload.model_dump()
    result = await engine.execute(
        user_id=user["id"], account=acc, signal=signal,
        max_concurrent=0, cfg_account_id=str(acc["_id"]),
    )
    if result.get("blocked"):
        raise HTTPException(status_code=422, detail=result)
    return result


@router.post("/accounts/{account_id}/verify")
async def crypto_verify(account_id: str, user=Depends(get_current_user)):
    """Lightweight key health probe — fetch_balance no-op."""
    db = get_db()
    acc = await _load_user_account(db, user, account_id)
    try:
        async with CCXTClient(acc) as client:
            bal = await client.fetch_balance()
        ok = True
        msg = "Keys verified."
        usdt = float((bal.get("total") or {}).get("USDT") or 0)
    except Exception as e:  # noqa: BLE001
        ok = False
        logger.warning("exchange connect check failed: %s", e)
        msg = "connection_failed"
        usdt = 0.0
    return {"ok": ok, "message": msg, "usdt_total": usdt,
            "testnet": acc.get("testnet", True)}


@router.get("/status")
async def crypto_status(user=Depends(get_current_user)):
    """Surface the master live-toggle state for the UI."""
    _ = user
    return {
        "live_enabled": _live_enabled(),
        "default_testnet": True,
    }


@router.get("/exchanges")
async def crypto_exchanges(refresh: bool = False, user=Depends(get_current_user)):
    """List of CCXT exchanges supported by this deployment, with the metadata
    the UI needs to render the Add-Account dropdown + conditional fields.

    Includes a **reachability probe** per exchange so the UI can flag which
    APIs the cluster's outbound IP can actually talk to (e.g. Binance global
    is 451-geoblocked from US-hosted clusters). Cached for 5 min unless
    `?refresh=true`.
    """
    _ = user
    reachability = await check_reachability(force=refresh)

    # Pick a smart default: prefer the configured DEFAULT (binance) if it's
    # reachable, otherwise fall back to the first reachable exchange so the
    # UI never preselects a known-broken option.
    default_id = DEFAULT_EXCHANGE_ID
    if reachability and not reachability.get(default_id, {}).get("reachable", False):
        for eid in SUPPORTED_EXCHANGES:
            if reachability.get(eid, {}).get("reachable"):
                default_id = eid
                break

    return {
        "default": default_id,
        "exchanges": [
            {
                "id": eid,
                "label": meta["label"],
                "requires_passphrase": meta["passphrase"],
                "supports_sandbox": meta["sandbox"],
                "default_quote": meta.get("default_quote", "USDT"),
                "reachable": reachability.get(eid, {}).get("reachable"),
                "reach_error": reachability.get(eid, {}).get("error"),
            }
            for eid, meta in EXCHANGES.items()
        ],
    }
