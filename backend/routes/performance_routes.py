"""Verified live performance — broker-truth statistics computed ONLY from
broker_deals (the EA-reported deal ledger), never from estimated P&L.
Includes a data-integrity stamp and an optional revocable public share link."""
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_user
from broker_env import broker_environment as _broker_env
from database import get_db
from differentiation import (KEY_ID, feature_evidence, perf_attestation,
                             verify_attestation)

router = APIRouter(prefix="/performance", tags=["performance"])
public_router = APIRouter(prefix="/public", tags=["public-performance"])


async def _verified_payload(db, user_id: str, mask: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    accounts = {}
    async for a in db.accounts.find(
            {"user_id": user_id, "status": {"$ne": "deleted"}},
            {"label": 1, "broker": 1, "mode": 1, "account_type": 1,
             "broker_server": 1, "server": 1, "broker_environment": 1,
             "last_heartbeat": 1}):
        accounts[str(a["_id"])] = a

    per = {}
    daily = {}
    total = {"net": 0.0, "wins": 0, "losses": 0, "deals": 0,
             "first": None, "last": None}
    async for d in db.broker_deals.find(
            {"user_id": user_id},
            {"account_id": 1, "deal_time": 1, "profit": 1, "commission": 1,
             "swap": 1, "deal_entry": 1}).sort("deal_time", 1).limit(20000):
        realized = (float(d.get("profit") or 0)
                    + float(d.get("commission") or 0)
                    + float(d.get("swap") or 0))
        ts = d.get("deal_time")
        try:
            day = datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            continue
        aid = d.get("account_id")
        p = per.setdefault(aid, {"net": 0.0, "wins": 0, "losses": 0,
                                 "deals": 0, "first": None, "last": None})
        for row in (p, total):
            row["net"] += realized
            row["deals"] += 1
            row["first"] = row["first"] or day
            row["last"] = day
        if d.get("deal_entry") == "out":
            outcome = "wins" if realized > 0 else "losses"
            p[outcome] += 1
            total[outcome] += 1
        daily[day] = daily.get(day, 0.0) + realized

    curve, cum, peak, max_dd = [], 0.0, 0.0, 0.0
    for day in sorted(daily):
        cum += daily[day]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        curve.append({"date": day, "net": round(daily[day], 2),
                      "cum": round(cum, 2)})

    def _stats(row):
        closed = row["wins"] + row["losses"]
        return {"net_pnl": round(row["net"], 2), "deals": row["deals"],
                "closed_positions": closed,
                "win_rate": (round(row["wins"] / closed * 100, 1)
                             if closed else None),
                "first_deal": row["first"], "last_deal": row["last"]}

    account_rows = []
    for i, (aid, p) in enumerate(sorted(per.items(),
                                        key=lambda kv: -kv[1]["net"])):
        a = accounts.get(aid, {})
        account_rows.append({
            "label": (f"ACCOUNT-{i + 1}" if mask
                      else (a.get("label") or f"ACCOUNT-{i + 1}")),
            "broker": a.get("broker"),
            "mode": a.get("mode"),
            # audit v5 P0-3 — server-owned environment; never derive "live"
            # from `not paper` on any surface
            "environment": (_broker_env(a) if a else "UNKNOWN"),
            **_stats(p)})

    # Integrity stamp — how much of the record is broker-verified truth.
    trades_closed = await db.trades.count_documents(
        {"user_id": user_id, "status": "closed", "origin": "auto"})
    verified = await db.trades.count_documents(
        {"user_id": user_id, "status": "closed", "origin": "auto",
         "broker_deal_id": {"$ne": None}})
    estimated = await db.trades.count_documents(
        {"user_id": user_id, "status": "closed", "origin": "auto",
         "$or": [{"pnl_estimated": True}, {"pnl_unknown": True}]})
    hb_age = None
    for a in accounts.values():
        try:
            d = datetime.fromisoformat(str(a.get("last_heartbeat")))
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            age = int((now - d).total_seconds())
            hb_age = age if hb_age is None else min(hb_age, age)
        except Exception:
            pass
    integrity = {
        "source": "broker_deals",
        "closed_trades": trades_closed,
        "broker_verified_trades": verified,
        "verified_pct": (round(verified / trades_closed * 100, 1)
                         if trades_closed else None),
        "estimated_or_unknown_excluded": estimated,
        "freshest_heartbeat_age_sec": hb_age,
    }

    from chart_provenance import build as provenance
    return {"generated_at": now.isoformat(),
            "overall": _stats(total),
            "max_drawdown": round(max_dd, 2),
            "equity_curve": curve[-365:],
            "accounts": account_rows,
            "integrity": integrity,
            "provenance": provenance(provider="broker_deals", source_kind="broker_reconciled", points=curve[-365:],
                                     time_key="date", expected_interval_s=86400, as_of=now,
                                     reconciliation_id=f"hb_age_{hb_age}s" if hb_age is not None else None,
                                     note="broker-confirmed deals only; estimated/unknown outcomes excluded")}


ATTESTATION_POLICY_VERSION = "attest-v2"
ATTESTATION_MAX_DATA_AGE_S = 6 * 3600


def _attestation_environment(a: dict) -> str:
    """Audit #4 P3 — a public LIVE claim needs EVIDENCE, not a heuristic
    default: LIVE only when the classifier says LIVE AND the EA identity
    chain verified the broker server. Anything ambiguous → UNKNOWN (which
    the gate refuses). Demo/paper classifications pass through."""
    env = _broker_env(a) or "UNKNOWN"
    if env != "LIVE":
        return env
    if a.get("mode") == "paper" or not a.get("verified_identity"):
        return "UNKNOWN"
    return "LIVE"


async def _attestation_gate(db, user_id: str) -> list:
    """Review P1 / audit P1-2 — an attestation is a signed claim of truth.
    PROHIBITED while P&L is UNRECONCILED, position truth is not FRESH on an
    enabled account, the dataset includes synthetic/test accounts, any
    enabled account is not classified LIVE (DEMO/PAPER/UNKNOWN rows never
    back a live-performance claim), no broker deals exist, or the newest
    broker deal is older than the max data age."""
    reasons = []
    try:
        from routes.trade_routes import trade_stats
        stats = await trade_stats(user={"id": user_id})
        if (stats.get("reconciliation") or {}).get("status") \
                == "UNRECONCILED":
            reasons.append("PNL_UNRECONCILED")
    except Exception:  # noqa: BLE001 — fail closed
        reasons.append("PNL_RECONCILIATION_UNAVAILABLE")
    try:
        from state_contract import contract
        sc = await contract(db, user_id)
        rows = [r for r in sc["accounts"] if r.get("account_enabled")]
        if not rows:
            reasons.append("NO_ENABLED_ACCOUNTS")
        if any(r["position_truth"] != "FRESH" for r in rows):
            reasons.append("POSITION_TRUTH_NOT_FRESH")
    except Exception:  # noqa: BLE001
        reasons.append("POSITION_TRUTH_UNAVAILABLE")
    from synthetic_data import is_synthetic_account
    envs = set()
    async for a in db.accounts.find(
            {"user_id": user_id, "status": {"$ne": "deleted"}},
            {"label": 1, "user_id": 1, "synthetic": 1, "mode": 1,
             "account_type": 1, "server": 1, "broker_server": 1,
             "broker_environment": 1, "trading_enabled": 1,
             "verified_identity": 1}):
        if is_synthetic_account(a):
            reasons.append("SYNTHETIC_ACCOUNT_DATA")
        if a.get("trading_enabled") is True:
            envs.add(_attestation_environment(a))
    if envs - {"LIVE"}:
        reasons.append("NON_LIVE_ENVIRONMENT:" + ",".join(
            sorted(envs - {"LIVE"})))
    newest = await db.broker_deals.find_one(
        {"user_id": user_id}, {"deal_time": 1}, sort=[("deal_time", -1)])
    if not newest:
        reasons.append("NO_BROKER_DEALS")
    else:
        try:
            dt = newest["deal_time"]
            if isinstance(dt, (int, float)):
                dt = datetime.fromtimestamp(int(dt), tz=timezone.utc)
            elif isinstance(dt, str):
                dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            if age > ATTESTATION_MAX_DATA_AGE_S:
                reasons.append("BROKER_DATA_STALE")
        except Exception:  # noqa: BLE001 — unparseable = not fresh
            reasons.append("BROKER_DATA_AGE_UNKNOWN")
    # round 13 P2-05 — the books must equal the SIGNED broker statement to the cent
    try:
        from broker_statement_ledger import ledger_gate
        reasons += await ledger_gate(db, user_id)
    except Exception:  # noqa: BLE001 — fail closed
        reasons.append("STATEMENT_LEDGER_UNAVAILABLE")
    return sorted(set(reasons))


async def _attach_attestation(db, user_id: str, payload: dict) -> dict:
    blockers = await _attestation_gate(db, user_id)
    payload["attestation_policy_version"] = ATTESTATION_POLICY_VERSION
    if blockers:
        payload["attestation"] = None
        payload["attestation_blocked"] = {
            "reasons": blockers,
            "note": "Attestation withheld — a signed performance claim "
                    "requires reconciled P&L, FRESH position truth, "
                    "LIVE-classified accounts only, fresh broker deals and "
                    "a dataset free of synthetic/test accounts."}
    else:
        from broker_statement_ledger import ledger_snapshot
        payload["verified_ledger"] = await ledger_snapshot(db, user_id)   # r15 P1-03 — bound into the claim
        payload["attestation"] = perf_attestation(payload)
        payload["attestation_blocked"] = None
    return payload


@router.get("/verified")
async def verified(user=Depends(get_current_user)):
    db = get_db()
    payload = await _verified_payload(db, user["id"])
    payload = await _attach_attestation(db, user["id"], payload)
    share = await db.performance_shares.find_one(
        {"user_id": user["id"], "revoked": {"$ne": True}})
    payload["share"] = ({"share_id": share["share_id"],
                         "created_at": share.get("created_at")}
                        if share else None)
    return payload


@router.post("/share")
async def create_share(user=Depends(get_current_user)):
    """Create (or rotate) the public read-only share link. Audit P1-2:
    a PUBLIC financial claim is refused unless the attestation gate
    passes — the public page can never show unverified headline P&L."""
    db = get_db()
    blockers = await _attestation_gate(db, user["id"])
    if blockers:
        raise HTTPException(
            status_code=409,
            detail={"code": "attestation_gate_failed", "reasons": blockers,
                    "policy_version": ATTESTATION_POLICY_VERSION,
                    "message": "Public share refused — performance is "
                               "UNVERIFIED until every attestation gate "
                               "passes."})
    share_id = secrets.token_urlsafe(16)
    await db.performance_shares.update_many(
        {"user_id": user["id"]}, {"$set": {"revoked": True}})
    await db.performance_shares.insert_one({
        "user_id": user["id"], "share_id": share_id, "revoked": False,
        "created_at": datetime.now(timezone.utc).isoformat()})
    return {"share_id": share_id}


@router.delete("/share")
async def revoke_share(user=Depends(get_current_user)):
    db = get_db()
    res = await db.performance_shares.update_many(
        {"user_id": user["id"], "revoked": {"$ne": True}},
        {"$set": {"revoked": True}})
    return {"revoked": res.modified_count > 0}


@public_router.get("/performance/{share_id}")
async def public_performance(share_id: str):
    """Unauthenticated read-only verified track record (masked labels)."""
    db = get_db()
    share = await db.performance_shares.find_one(
        {"share_id": share_id, "revoked": {"$ne": True}})
    if not share:
        # no-store on the negative path too: a revoked share must never be served from an edge/browser cache
        raise HTTPException(status_code=404, detail="Share link not found or revoked",
                            headers={"Cache-Control": "no-store, max-age=0"})
    payload = await _verified_payload(db, share["user_id"], mask=True)
    payload = await _attach_attestation(db, share["user_id"], payload)
    payload["shared"] = True
    if payload.get("attestation_blocked"):
        # audit P1-2 — an existing link whose gate no longer passes must
        # not present financial headline metrics as verified.
        payload["verification_status"] = "UNVERIFIED — NOT LIVE PERFORMANCE"
        payload["overall"] = None
        payload["max_drawdown"] = None
        payload["equity_curve"] = []
        for row in payload.get("accounts") or []:
            for k in ("net", "wins", "losses", "win_rate", "profit_factor"):
                row.pop(k, None)
    else:
        payload["verification_status"] = "VERIFIED"
    from fastapi.responses import JSONResponse
    return JSONResponse(payload, headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"})


class VerifyBody(BaseModel):
    payload_hash: str
    signature: str


@public_router.post("/performance/verify")
async def verify_performance(body: VerifyBody):
    """Anyone can verify a track record wasn't tampered with (Phase 8).
    Review P1-4: Ed25519 — independently verifiable without trusting this
    server (legacy HMAC attestations still accepted)."""
    from differentiation import LEGACY_HMAC_ACCEPTED_UNTIL
    from release_signing import public_key_b64
    return {"valid": verify_attestation(body.payload_hash, body.signature),
            "algo": "Ed25519(sha256-canonical-JSON); legacy HMAC accepted "
                    f"until {LEGACY_HMAC_ACCEPTED_UNTIL}",
            "legacy_hmac_accepted_until": LEGACY_HMAC_ACCEPTED_UNTIL,
            "public_key_b64": public_key_b64(),
            "key_id": KEY_ID}


@router.get("/evidence")
async def evidence(days: int = 30, user=Depends(get_current_user)):
    """Phase 9 evidence board — every feature measured from real data."""
    db = get_db()
    return await feature_evidence(db, user["id"], days=max(1, min(days, 365)))
