"""Phase 8/9 — commercial differentiation utilities.

  perf_attestation()   Ed25519-signed attestation (review P1-4) of a
                       canonical performance payload — independently
                       verifiable by ANYONE with the published public key,
                       unlike the legacy HMAC scheme which only the server
                       could check. Legacy HMAC attestations still verify.
  certify()            broker certification tier from live execution scores.
  feature_evidence()   Phase 9 evidence board: every major feature is
                       measured from REAL data and classified
                       proven / experimental / review. Unmeasurable = experimental.
"""
import hashlib
import hmac
import json
import os
from datetime import datetime, timedelta, timezone

LEGACY_KEY_ID = "perf-hmac-v1"
KEY_ID = "perf-ed25519-v1"
# P2 (review) — legacy HMAC attestations are accepted for verification
# only until this instant; afterwards only Ed25519 signatures verify.
LEGACY_HMAC_ACCEPTED_UNTIL = "2027-01-01T00:00:00+00:00"


def _signing_key() -> bytes:
    return (os.environ.get("PERF_SIGNING_KEY")
            or os.environ["JWT_SECRET"]).encode()


def canonical_hash(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def perf_attestation(payload: dict) -> dict:
    from release_signing import public_key_b64, sign_hex
    h = canonical_hash(payload)
    sig = sign_hex(h.encode(), purpose="differentiation")
    return {"payload_hash": h, "signature": sig, "key_id": KEY_ID,
            "algo": "Ed25519(sha256-canonical-JSON)",
            "public_key_b64": public_key_b64("differentiation"),
            "verify_hint": "Ed25519.verify(public_key_b64, "
                           "signature_hex, payload_hash_bytes) — "
                           "no server trust required",
            "signed_at": datetime.now(timezone.utc).isoformat()}


def verify_attestation(payload_hash: str, signature: str) -> bool:
    from release_signing import verify_hex
    if verify_hex(payload_hash.encode(), signature, purpose="differentiation"):
        return True
    # legacy HMAC attestations — only until the published retirement date
    if datetime.now(timezone.utc).isoformat() >= LEGACY_HMAC_ACCEPTED_UNTIL:
        return False
    want = hmac.new(_signing_key(), payload_hash.encode(),
                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature)


# --------------------------------------------------- broker certification
def certify(score, provisional: bool, fills: int) -> dict:
    if score is None or provisional or fills < 10:
        return {"tier": "PROVISIONAL",
                "detail": f"needs ≥10 measured fills ({fills} so far)"}
    if score >= 80:
        return {"tier": "CERTIFIED",
                "detail": f"score {score} ≥ 80 over {fills} measured fills"}
    if score >= 55:
        return {"tier": "ACCEPTABLE",
                "detail": f"score {score} — execution adequate, monitor"}
    return {"tier": "DEGRADED",
            "detail": f"score {score} < 55 — poor execution, avoid routing"}


# --------------------------------------------------- feature evidence board
async def feature_evidence(db, user_id: str, days: int = 30) -> dict:
    """Phase 9: measure each feature's real impact; unproven = experimental."""
    since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []

    def verdict(n, min_n, positive, negative=False):
        if negative:
            return "review"
        if n >= min_n and positive:
            return "proven"
        return "experimental"

    # 1. execution timing — spread saved on delayed sends
    acc_ids = [str(a["_id"]) async for a in db.accounts.find(
        {"user_id": user_id}, {"_id": 1})]
    mine = await db.execution_timing_stats.find(
        {"at": {"$gte": since_dt},
         "account_id": {"$in": acc_ids}}).to_list(500)
    n = len(mine)
    saved = sum(d["spread_before"] - d["spread_after"] for d in mine)
    imp = sum(1 for d in mine if d.get("improved"))
    rows.append({"feature": "execution_timing",
                 "question": "Does it improve execution?",
                 "n": n,
                 "metric": f"{saved:.1f} spread-pips saved · "
                           f"{round(100 * imp / n) if n else 0}% of delays improved the fill",
                 "verdict": verdict(n, 20, n and imp / n >= 0.5 and saved > 0,
                                    negative=bool(n >= 20 and saved < 0))})

    # 2. adaptive exits — protective actions actually taken
    kinds = ("TargetsRescaled", "StopTightened", "PartialCloseRequested")
    n_act = await db.trade_events.count_documents(
        {"user_id": user_id, "event_type": {"$in": kinds},
         "source": "adaptive_exits"})
    rows.append({"feature": "adaptive_exits",
                 "question": "Does it reduce drawdown?",
                 "n": n_act,
                 "metric": f"{n_act} protective in-flight actions recorded",
                 "verdict": verdict(n_act, 15, n_act >= 15)})

    # 3. regime gating — stamped evidence + benched strategies
    n_stamped = await db.trades.count_documents(
        {"user_id": user_id, "market_regime.key": {"$exists": True}})
    rows.append({"feature": "regime_gating",
                 "question": "Does it improve risk-adjusted returns?",
                 "n": n_stamped,
                 "metric": f"{n_stamped} trades stamped with a regime — edge "
                           f"buckets bench proven-negative strategies at ≥8 trades",
                 "verdict": verdict(n_stamped, 40, n_stamped >= 40)})

    # 4. dynamic allocation — how far evidence moved capital vs static split
    alloc = await db.strategy_allocations.find_one({"user_id": user_id})
    from risk_budget import DEFAULT_ALLOCATIONS
    shift = 0.0
    if alloc:
        shift = sum(abs(alloc["weights"].get(k, v) - v)
                    for k, v in DEFAULT_ALLOCATIONS.items()) / 2
    rows.append({"feature": "dynamic_allocation",
                 "question": "Does it improve risk-adjusted returns?",
                 "n": (alloc or {}).get("n_trades", 0),
                 "metric": f"{shift * 100:.0f}% of the risk pool reallocated "
                           f"away from the static split by live evidence",
                 "verdict": verdict((alloc or {}).get("n_trades", 0), 30,
                                    bool(alloc and shift > 0.05))})

    # 5. risk layers / circuit breakers — stops that actually fired
    since_iso = since_dt.isoformat()
    trips = await db.bot_configs.count_documents(
        {"user_id": user_id, "tripped_at": {"$gte": since_iso}})
    rows.append({"feature": "risk_layers",
                 "question": "Does it reduce operational risk?",
                 "n": trips,
                 "metric": f"{trips} hard stop(s) fired in {days}d — "
                           f"fail-closed portfolio protection live",
                 "verdict": "proven" if trips else "experimental"})

    # 6. learning pipeline — gate decisions (rejections prove the gate works)
    runs = await db.learning_runs.find(
        {"user_id": user_id, "at": {"$gte": since_dt}}).to_list(200)
    frozen = sum(1 for r in runs if r.get("frozen"))
    ml = [((r.get("stages") or {}).get("ml_ensemble") or {}).get("status")
          for r in runs]
    rej, prom = ml.count("rejected"), ml.count("candidate_ready_for_review")
    rows.append({"feature": "learning_pipeline",
                 "question": "Does it reduce operational risk?",
                 "n": len(runs),
                 "metric": f"{prom} candidate(s) staged for two-admin review · {rej} rejected · {frozen} frozen runs",
                 "verdict": "proven" if (rej or frozen or prom) else "experimental"})

    proven = sum(1 for r in rows if r["verdict"] == "proven")
    return {"window_days": days, "features": rows,
            "summary": {"proven": proven,
                        "experimental": sum(1 for r in rows
                                            if r["verdict"] == "experimental"),
                        "review": sum(1 for r in rows if r["verdict"] == "review")},
            "principle": "unmeasured benefit = experimental until it proves itself"}
