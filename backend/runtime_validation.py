"""iter-157 — Runtime validation harness: security/infra/billing scenario
proofs that exercise REAL production gates against synthetic, self-cleaning
fixtures. Complements validation_harness.py (MT5 EA campaign) and
chaos_drills.py (failure drills). Results land in db.validation_runs
(mode="runtime") — the release-readiness verdict already consumes that
collection.
"""
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("runtime-validation")


def _now():
    return datetime.now(timezone.utc)


# ─── infrastructure ──────────────────────────────────────────────────
async def s_lease_expiration(db):
    """An expired execution lease must be adoptable by another worker with a
    bumped fencing epoch, and the stale owner must lose ownership."""
    from scalp.engine import acquire_account_lease
    acct = f"rv-lease-{uuid.uuid4().hex[:8]}"
    try:
        assert await acquire_account_lease(db, acct, worker_id="rv-A"), \
            "worker A could not acquire a fresh lease"
        doc = await db.scalp_owners.find_one({"account_id": acct})
        epoch_a = int(doc.get("lease_epoch") or 0)
        # expire A's lease
        await db.scalp_owners.update_one(
            {"account_id": acct},
            {"$set": {"lease_until": (_now() - timedelta(seconds=5)).isoformat()}})
        assert await acquire_account_lease(db, acct, worker_id="rv-B"), \
            "worker B could not adopt the expired lease"
        doc = await db.scalp_owners.find_one({"account_id": acct})
        assert doc["worker_id"] == "rv-B", "ownership did not transfer"
        assert int(doc["lease_epoch"]) == epoch_a + 1, \
            "fencing epoch was not bumped on ownership change"
        assert not await acquire_account_lease(db, acct, worker_id="rv-A"), \
            "stale worker A re-acquired a live lease"
        return True, (f"expired lease adopted by new worker; fencing epoch "
                      f"{epoch_a}→{epoch_a + 1}; stale owner rejected")
    finally:
        await db.scalp_owners.delete_one({"account_id": acct})


async def s_heartbeat_timeout(db):
    """A stale EA heartbeat must block order submission (the same gate the
    scalp engine consults before every broker order)."""
    from scalp.engine import broker_state_stale_reason
    stale = {"last_heartbeat": (_now() - timedelta(minutes=30)).isoformat(),
             "status": "connected"}
    fresh = {"last_heartbeat": _now().isoformat(), "status": "connected"}
    r_stale = broker_state_stale_reason(stale)
    r_fresh = broker_state_stale_reason(fresh)
    assert r_stale and "stale" in r_stale, "stale heartbeat NOT blocked"
    assert r_fresh is None, f"fresh heartbeat wrongly blocked: {r_fresh}"
    assert broker_state_stale_reason({}) is not None, \
        "missing heartbeat NOT blocked"
    return True, f"stale heartbeat blocked ({r_stale}); fresh allowed"


async def s_artifact_verification(db):
    """The signed artifact manifest must verify, and any tampering must be
    detected (the exact check the host agent performs before installing)."""
    import json
    import release_signing
    from vps_pathb import build_artifact_manifest
    m = build_artifact_manifest()
    body = json.dumps({k: m[k] for k in ("artifacts", "update_policy")},
                      sort_keys=True, separators=(",", ":"),
                      default=str).encode()
    assert release_signing.verify_hex(body, m["signature"]["value"]), \
        "genuine manifest failed signature verification"
    tampered = json.loads(body)
    tampered["artifacts"][0]["sha256"] = "0" * 64
    tam_body = json.dumps(tampered, sort_keys=True,
                          separators=(",", ":")).encode()
    assert not release_signing.verify_hex(tam_body, m["signature"]["value"]), \
        "TAMPERED manifest passed signature verification"
    return True, (f"Ed25519 manifest verified; tampered sha256 rejected; "
                  f"{len(m['artifacts'])} artifacts content-addressed")


# ─── promotion gates ─────────────────────────────────────────────────
async def s_promotion_gates(db):
    """Shadow→Demo→Live promotion rules: DEGRADED blocks all live modes,
    PROVISIONAL blocks autonomous_live, CERTIFIED passes."""
    from operational_modes import evaluate_promotion
    degraded = evaluate_promotion("supervised_live",
                                  [{"account": "rv1", "tier": "DEGRADED"}])
    assert not degraded["allowed"], "DEGRADED cert allowed into live mode"
    prov_auto = evaluate_promotion("autonomous_live",
                                   [{"account": "rv1", "tier": "PROVISIONAL"}])
    assert not prov_auto["allowed"], \
        "PROVISIONAL cert allowed into autonomous_live"
    prov_sup = evaluate_promotion("supervised_live",
                                  [{"account": "rv1", "tier": "PROVISIONAL"}])
    assert prov_sup["allowed"] and prov_sup["warnings"], \
        "PROVISIONAL should reach supervised_live with a warning"
    cert = evaluate_promotion("autonomous_live",
                              [{"account": "rv1", "tier": "CERTIFIED"}])
    assert cert["allowed"], "CERTIFIED cert blocked from autonomous_live"
    return True, ("DEGRADED blocked, PROVISIONAL capped at supervised_live "
                  "(with warning), CERTIFIED fully promoted")


# ─── security ────────────────────────────────────────────────────────
async def s_expired_token(db):
    """An expired JWT must be rejected by the production decoder."""
    import jwt as pyjwt
    from auth import decode_token, get_jwt_secret, JWT_ALGORITHM
    expired = pyjwt.encode(
        {"sub": "rv-user", "email": "rv@example.com",
         "exp": _now() - timedelta(minutes=5)},
        get_jwt_secret(), algorithm=JWT_ALGORITHM)
    try:
        decode_token(expired)
        return False, "EXPIRED token was accepted"
    except pyjwt.ExpiredSignatureError:
        pass
    wrong_key = pyjwt.encode(
        {"sub": "rv-user", "exp": _now() + timedelta(minutes=5)},
        "not-the-real-secret", algorithm=JWT_ALGORITHM)
    try:
        decode_token(wrong_key)
        return False, "token signed with WRONG KEY was accepted"
    except pyjwt.InvalidTokenError:
        pass
    return True, "expired token rejected; forged-signature token rejected"


async def s_replay_protection(db):
    """A consumed (already-rotated) refresh token must be rejected AND its
    whole session family revoked — the token-theft containment path."""
    from fastapi import HTTPException
    from security import consume_and_rotate
    jti = f"rv-replay-{uuid.uuid4().hex[:8]}"
    fam = f"fam-{jti}"
    now = _now().isoformat()
    try:
        await db.auth_sessions.insert_one({
            "jti": jti, "session_id": "rv", "family": fam,
            "user_id": "rv-user", "token_hash": None,
            "created_at": now, "last_used_at": now,
            "expires_at": _now() + timedelta(days=1),
            "revoked": False, "consumed": False})
        first = await consume_and_rotate(db, {"jti": jti}, "tok-1")
        assert first and first["jti"] != jti, "rotation did not issue new jti"
        try:
            await consume_and_rotate(db, {"jti": jti}, "tok-1")
            return False, "REPLAYED refresh token was accepted"
        except HTTPException as e:
            assert e.status_code == 401
        n_revoked = await db.auth_sessions.count_documents(
            {"family": fam, "revoked": True})
        assert n_revoked >= 1, "family was not revoked after replay"
        return True, ("rotation ok; replay rejected with 401; "
                      f"{n_revoked} family session(s) revoked")
    finally:
        await db.auth_sessions.delete_many({"family": fam})


async def s_unauthorized_api(db):
    """Admin/ops surfaces must reject unauthenticated requests outright."""
    import httpx
    async with httpx.AsyncClient(base_url="http://localhost:8001",
                                 timeout=10) as client:
        checks = []
        for path in ("/api/admin/ops-console", "/api/admin/users",
                     "/api/admin/settings/turnstile"):
            r = await client.get(path)
            checks.append((path, r.status_code))
            assert r.status_code in (401, 403), \
                f"{path} returned {r.status_code} without credentials"
        return True, "; ".join(f"{p}→{c}" for p, c in checks)


async def s_turnstile_fail_closed(db):
    """A missing/invalid Turnstile token must be a client fault (rejected),
    never mistaken for a Cloudflare outage (which fails open)."""
    from turnstile_gate import verify_token
    res = await verify_token("")
    assert res["ok"] is False and res["outage"] is False, \
        "empty Turnstile token not treated as client fault"
    return True, "empty token → client fault (fail-closed path confirmed)"


# ─── billing ─────────────────────────────────────────────────────────
async def s_billing_refund_idempotent(db):
    """Refund revocation must apply exactly once (idempotent) — a replayed
    webhook can never double-revoke."""
    from subscription_service import revoke_payment
    sid = f"rv-sess-{uuid.uuid4().hex[:8]}"
    uid = f"rv-user-{uuid.uuid4().hex[:8]}"
    try:
        await db.payment_transactions.insert_one({
            "session_id": sid, "user_id": uid, "plan_id": "rv-plan",
            "applied": True, "skipped_reason": "runtime-validation synthetic",
            "payment_status": "paid",
            "created_at": _now().isoformat()})
        first = await revoke_payment(sid, reason="runtime-validation")
        assert first is not None, "first revoke did not apply"
        txn = await db.payment_transactions.find_one({"session_id": sid})
        assert txn["revoked"] is True and txn["payment_status"] == "refunded"
        second = await revoke_payment(sid, reason="runtime-validation")
        assert second is None, "second revoke applied — NOT idempotent"
        return True, "refund revoked once; replayed revoke was a no-op"
    finally:
        await db.payment_transactions.delete_many({"session_id": sid})


SCENARIOS = {
    "lease_expiration": s_lease_expiration,
    "heartbeat_timeout": s_heartbeat_timeout,
    "artifact_verification": s_artifact_verification,
    "promotion_gates": s_promotion_gates,
    "expired_token": s_expired_token,
    "replay_protection": s_replay_protection,
    "unauthorized_api": s_unauthorized_api,
    "turnstile_fail_closed": s_turnstile_fail_closed,
    "billing_refund_idempotent": s_billing_refund_idempotent,
}


async def run_runtime_validation(db, scenarios=None, actor="system") -> dict:
    run_id = f"rtv-{uuid.uuid4().hex[:10]}"
    names = [n for n in (scenarios or list(SCENARIOS)) if n in SCENARIOS]
    results = []
    for name in names:
        try:
            ok, notes = await SCENARIOS[name](db)
        except AssertionError as e:
            ok, notes = False, f"assertion failed: {e}"
        except Exception as e:  # noqa: BLE001
            ok, notes = False, f"harness exception {type(e).__name__}: {e}"[:400]
        results.append({"scenario": name,
                        "status": "pass" if ok else "fail",
                        "notes": str(notes)[:1000]})
    run_doc = {"run_id": run_id, "mode": "runtime",
               "started_by": actor, "recorded": True,
               "results": results,
               "passed": sum(1 for r in results if r["status"] == "pass"),
               "failed": sum(1 for r in results if r["status"] == "fail"),
               "at": _now()}
    await db.validation_runs.insert_one(dict(run_doc))
    run_doc.pop("_id", None)
    return run_doc
